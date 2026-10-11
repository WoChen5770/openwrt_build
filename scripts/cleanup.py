#!/usr/bin/env python3
"""Conservative retention for this project's releases and build caches only."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess

if __package__:
    from .build import ROOT, read_json
else:
    from build import ROOT, read_json


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def settings():
    values = read_json(ROOT / 'build-settings.json')
    for key in ('releases_keep_per_profile', 'release_recovery_days', 'cache_keep_generations', 'cache_min_age_days'):
        if type(values[key]) is not int or values[key] < 1:
            raise ValueError('Retention must be a positive integer: ' + key)
    if values['release_recovery_days'] < values['artifact_retention_days']:
        raise ValueError('Release recovery protection must cover the artifact retention window')
    return values


def release_candidates(releases, profiles, values, now):
    groups = defaultdict(list)
    for release in releases:
        match = re.fullmatch(r'([a-z0-9-]+)-r([1-9][0-9]*)-([1-9][0-9]*)', release['tag_name'])
        if (not match or match[1] not in profiles or release['draft'] or release.get('prerelease')
                or not re.search(r'<!-- build-fingerprint:[0-9a-f]{64} -->', release.get('body') or '')
                or not release.get('published_at')):
            continue
        groups[match[1]].append(release)
    cutoff = now - timedelta(days=values['release_recovery_days'])
    result = []
    for items in groups.values():
        items.sort(key=lambda r: (timestamp(r['published_at']), r['id']), reverse=True)
        result.extend(r for r in items[values['releases_keep_per_profile']:]
                      if timestamp(r['published_at']) < cutoff)
    return result


def cache_candidates(caches, profiles, values, now):
    groups = defaultdict(list)
    for cache in caches:
        for profile in sorted(profiles, key=len, reverse=True):
            patterns = (
                ('firmware', r'firmware-Linux-' + re.escape(profile) + r'-[0-9a-f]{64}-[0-9]+-[0-9]+'),
                ('toolchain', r'toolchain-v1-Linux-ubuntu24-' + re.escape(profile) + r'-[0-9a-f]{24}-[0-9a-f]{40}-[0-9]+-[0-9]+'),
            )
            kind = next((kind for kind, pattern in patterns if re.fullmatch(pattern, cache['key'])), None)
            if kind:
                groups[(profile, kind, cache['ref'])].append(cache)
                break
    cutoff = now - timedelta(days=values['cache_min_age_days'])
    result = []
    for items in groups.values():
        items.sort(key=lambda c: (timestamp(c['created_at']), c['id']), reverse=True)
        result.extend(c for c in items[values['cache_keep_generations']:]
                      if timestamp(c['created_at']) < cutoff and timestamp(c['last_accessed_at']) < cutoff)
    return result


def api(endpoint, paginate=False):
    args = ['gh', 'api', endpoint]
    if paginate:
        args += ['--paginate', '--slurp']
    return json.loads(subprocess.check_output(args, text=True))


def active_builds(base):
    active = []
    for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
        for page in api(base + '/actions/runs?status=' + status + '&per_page=100', True):
            active.extend(r for r in page['workflow_runs']
                          if r['path'] in ('.github/workflows/build.yml', '.github/workflows/release.yml'))
    return active


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Actually delete candidates; default is dry run')
    args = parser.parse_args()
    repository = os.environ['GITHUB_REPOSITORY']
    base = 'repos/' + repository
    values = settings()
    profiles = read_json(ROOT / 'profiles.json')['profiles']
    # Do not disrupt a compilation, publication or publication recovery.
    if active_builds(base):
        print('Build/recovery active: cleanup skipped')
        return
    now = datetime.now(timezone.utc)
    releases = [r for page in api(base + '/releases?per_page=100', True) for r in page]
    caches = [c for page in api(base + '/actions/caches?per_page=100', True) for c in page['actions_caches']]
    for release in release_candidates(releases, profiles, values, now):
        print('Delete release and its exact tag: ' + release['tag_name'])
        if args.apply:
            subprocess.run(['gh', 'release', 'delete', release['tag_name'], '--cleanup-tag', '--yes', '--repo', repository], check=True)
    for cache in cache_candidates(caches, profiles, values, now):
        print('Delete cache: ' + str(cache['id']) + ' ' + cache['key'])
        if args.apply:
            subprocess.run(['gh', 'api', '--method', 'DELETE', base + '/actions/caches/' + str(cache['id'])], check=True)
    if not args.apply:
        print('Dry run: nothing deleted')


if __name__ == '__main__':
    main()
