#!/usr/bin/env python3
"""Publish verified build artifacts, including recovery without recompilation."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def gh_json(endpoint, paginated=False):
    args = ['gh', 'api', endpoint]
    if paginated:
        args += ['--paginate', '--slurp']
    return json.loads(subprocess.check_output(args, text=True))


def context():
    repository = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['SOURCE_RUN_ID']
    if not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Invalid source run ID')
    repo = gh_json('repos/' + repository)
    run = gh_json('repos/' + repository + '/actions/runs/' + run_id)
    if (run['path'] != '.github/workflows/build.yml'
            or run['head_repository']['full_name'] != repository
            or run['head_branch'] != repo['default_branch']
            or run['event'] not in ('workflow_dispatch', 'schedule')):
        raise ValueError('Only trusted default-branch firmware builds may be published')
    return repository, repo, run


def plan():
    repository, _, run = context()
    profiles = json.loads((ROOT / 'profiles.json').read_text())['profiles']
    selected = os.environ.get('PROFILE', 'all')
    automatic = os.environ.get('AUTO_RECOVER') == 'true'
    if selected != 'all' and selected not in profiles:
        raise ValueError('Unknown profile')
    attempt = str(run['run_attempt'])
    base = 'repos/' + repository + '/actions/runs/' + str(run['id'])
    pages = gh_json(base + '/attempts/' + attempt + '/jobs?per_page=100', True)
    successful, failed_publish = set(), set()
    for page in pages:
        for job in page['jobs']:
            match = re.match(r'^build \(([^,]+),.* / (compile|publish)$', job['name'])
            if match and match[2] == 'compile' and job['conclusion'] == 'success':
                successful.add(match[1])
            if match and match[2] == 'publish' and job['conclusion'] == 'failure':
                failed_publish.add(match[1])
    # Opted-out (publish=false) builds have no failed publisher and stay unpublished.
    if automatic:
        successful &= failed_publish
    pages = gh_json(base + '/artifacts?per_page=100', True)
    available = {a['name'] for page in pages for a in page['artifacts'] if not a['expired']}
    matrix = {'include': [dict(profile=name, source_run_id=str(run['id']), source_run_attempt=attempt)
                          for name in profiles if name in successful
                          and selected in ('all', name) and 'firmware-' + name + '-' + attempt in available]}
    if not matrix['include'] and not automatic:
        raise ValueError('No successful, unexpired firmware artifacts for this selection')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix) + '\n')
        output.write('has_releases=' + str(bool(matrix['include'])).lower() + '\n')
    print(json.dumps(matrix))


def validate_info(info, run, profile):
    if (info['profile_id'] != profile or str(info['run_id']) != str(run['id'])
            or info['ci_commit'] != run['head_sha']
            or not re.fullmatch(r'[0-9a-f]{40}', info['ci_commit'])
            or not re.fullmatch(r'[0-9a-f]{64}', info['fingerprint'])):
        raise ValueError('Artifact provenance does not match its source build')


def publish(directory):
    repository, repo, run = context()
    profile = os.environ['PROFILE']
    profiles = json.loads((ROOT / 'profiles.json').read_text())['profiles']
    attempt = os.environ['SOURCE_RUN_ATTEMPT']
    if profile not in profiles or attempt != str(run['run_attempt']):
        raise ValueError('Invalid profile or stale build attempt')
    directory = directory.resolve()
    subprocess.run(['sha256sum', '-c', 'sha256sums'], cwd=directory, check=True)
    info = json.loads((directory / 'build-info.json').read_text())
    validate_info(info, run, profile)
    marker = '<!-- build-fingerprint:' + info['fingerprint'] + ' -->'
    notes = (directory / 'release-notes.md').read_text()
    if marker not in notes:
        raise ValueError('Release notes fingerprint does not match build metadata')
    notes += '\n构建仓库提交：`' + info['ci_commit'] + '`。\n'
    notes += 'Release 标签为发布索引；实际构建提交以 `build-info.json` 为准。\n'
    tag = profile + '-r' + str(run['id']) + '-' + attempt
    existing = subprocess.run(['gh', 'api', 'repos/' + repository + '/releases/tags/' + tag],
                              capture_output=True, text=True)
    assets = sorted(p for p in directory.iterdir() if p.is_file())
    if existing.returncode == 0:
        release = json.loads(existing.stdout)
        if marker not in (release['body'] or ''):
            raise ValueError('Existing release belongs to a different build; refusing replacement')
        if not release['draft']:
            remote = {a['name']: a['size'] for a in release['assets']}
            if any(remote.get(p.name) != p.stat().st_size for p in assets):
                raise ValueError('Published release is incomplete; refusing replacement')
            print('Already published: ' + release['html_url'])
            return
        # Resume only this run's unfinished draft; never replace a published release.
        subprocess.run(['gh', 'release', 'upload', tag, *map(str, assets), '--clobber',
                        '--repo', repository], check=True)
    else:
        if 'HTTP 404' not in existing.stderr:
            raise RuntimeError(existing.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            notes_file = Path(temporary) / 'notes.md'
            notes_file.write_text(notes)
            # A historical CI SHA can require workflows:write after main changes.
            # Target the default branch; record the true build SHA separately above.
            subprocess.run(['gh', 'release', 'create', tag, *map(str, assets), '--draft',
                            '--target', repo['default_branch'], '--title', profile + ' · ' + info['built_at'][:10],
                            '--notes-file', str(notes_file), '--repo', repository], check=True)
    subprocess.run(['gh', 'release', 'edit', tag, '--draft=false', '--repo', repository], check=True)
    print('Published: https://github.com/' + repository + '/releases/tag/' + tag)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    publish_parser = commands.add_parser('publish')
    publish_parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    plan() if args.command == 'plan' else publish(args.directory)
