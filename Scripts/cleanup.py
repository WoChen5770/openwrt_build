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
    from .build import ROOT, ccache_group, read_toml
else:
    from build import ROOT, ccache_group, read_toml


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def settings():
    values = read_toml(ROOT / 'Config/build-settings.toml')
    for key in ('releases_keep_per_profile', 'cache_keep_generations'):
        if type(values[key]) is not int or values[key] < 1:
            raise ValueError('Retention must be a positive integer: ' + key)
    for key in ('release_recovery_days', 'cache_min_age_days'):
        if type(values[key]) is not int or values[key] < 0:
            raise ValueError('Retention must be a non-negative integer: ' + key)
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
                      if values['release_recovery_days'] == 0 or timestamp(r['published_at']) < cutoff)
    return result


def cache_candidates(caches, profiles, values, now):
    groups = defaultdict(list)
    profile_groups = {name: ccache_group(name, profiles[name] if isinstance(profiles, dict) else {})
                      for name in profiles}
    compiler_groups = set(profile_groups.values())
    installed = re.compile(r'^(toolchain|hostpkg)-v2-Linux-ubuntu24-([a-z0-9-]+)-[0-9a-f]{24}$')
    for cache in caches:
        if cache.get('size_in_bytes', 1) <= 0:
            continue
        shared = next((group for group in compiler_groups
                       if re.fullmatch(r'ccache-v2-Linux-ubuntu24-' + re.escape(group) + r'-[0-9]+-[0-9]+',
                                       cache['key'])), None)
        if shared:
            groups[(shared, 'ccache-v2', cache['ref'])].append(cache)
            continue
        match = installed.fullmatch(cache['key'])
        if match:
            kind, owner = match.groups()
            if kind == 'toolchain':
                owner = profile_groups.get(owner, owner if owner in compiler_groups else None)
            elif owner not in profiles:
                owner = None
            if owner:
                groups[(owner, kind, cache['ref'])].append(cache)
                continue
        for profile in sorted(profiles, key=len, reverse=True):
            patterns = (
                ('firmware', r'firmware-Linux-' + re.escape(profile) + r'-[0-9a-f]{64}-[0-9]+-[0-9]+'),
                ('toolchain-v1', r'toolchain-v1-Linux-ubuntu24-' + re.escape(profile) + r'-[0-9a-f]{24}-[0-9a-f]{40}-[0-9]+-[0-9]+'),
            )
            kind = next((kind for kind, pattern in patterns if re.fullmatch(pattern, cache['key'])), None)
            if kind:
                groups[(profile, kind, cache['ref'])].append(cache)
                break
    cutoff = now - timedelta(days=values['cache_min_age_days'])
    result = []
    for (_, kind, _), items in groups.items():
        # Stable installed caches can be reused after switching back to an older configuration.
        field = 'last_accessed_at' if kind in ('toolchain', 'hostpkg') else 'created_at'
        items.sort(key=lambda c: (timestamp(c[field]), timestamp(c['created_at']), c['id']), reverse=True)
        result.extend(c for c in items[values['cache_keep_generations']:]
                      if values['cache_min_age_days'] == 0
                      or (timestamp(c['created_at']) < cutoff and timestamp(c['last_accessed_at']) < cutoff))
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
                          if r['path'] == '.github/workflows/build.yml')
    return active


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Actually delete candidates; default is dry run')
    args = parser.parse_args()
    repository = os.environ['GITHUB_REPOSITORY']
    base = 'repos/' + repository
    values = settings()
    profiles = read_toml(ROOT / 'Config/profiles.toml')['profiles']
    # The unified build workflow includes both compilation and publication.
    if active_builds(base):
        print('Build/publication active: cleanup skipped')
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
