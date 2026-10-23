#!/usr/bin/env python3
"""Select and publish verified firmware artifacts from the build workflow."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

if __package__:
    from .build import ROOT, read_toml
else:
    from build import ROOT, read_toml


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


def latest_jobs(repository, run):
    # A partial rerun keeps successful producer jobs from an older attempt.
    # Read job history rather than treating the run's latest attempt as theirs.
    base = 'repos/' + repository + '/actions/runs/' + str(run['id'])
    pages = gh_json(base + '/jobs?filter=all&per_page=100', True)
    latest = {}
    for page in pages:
        for job in page['jobs']:
            # Matrix display names may include the kernel compression variant.
            match = re.fullmatch(
                r'([a-z0-9-]+)(?: / [a-z0-9-]+)? / (compile|publish)(?: / \2)?',
                job['name'])
            if not match:
                match = re.match(r'^build \(([^,]+),.*? / (compile|publish)(?: / \2)?$', job['name'])
            if not match:
                continue
            key = (match[1], match[2])
            order = (int(job['run_attempt']), int(job['id']))
            previous = latest.get(key)
            if previous is None or order > (int(previous['run_attempt']), int(previous['id'])):
                latest[key] = job
    return latest


def verify_build(repository, run, profile, attempt):
    if not re.fullmatch(r'[1-9][0-9]*', attempt):
        raise ValueError('Invalid source build attempt')
    job = latest_jobs(repository, run).get((profile, 'compile'))
    if not job or job['conclusion'] != 'success' or str(job['run_attempt']) != attempt:
        raise ValueError('No matching successful compilation; refusing stale or failed build')


def plan():
    repository, _, run = context()
    profiles = read_toml(ROOT / 'Config/profiles.toml')['profiles']
    selected = os.environ.get('PROFILE', 'all')
    if selected != 'all' and selected not in profiles:
        raise ValueError('Unknown profile')
    base = 'repos/' + repository + '/actions/runs/' + str(run['id'])
    jobs = latest_jobs(repository, run)
    pages = gh_json(base + '/artifacts?per_page=100', True)
    available = [a for page in pages for a in page['artifacts'] if not a['expired']]
    matrix = {'include': []}
    has_successful_build = False
    for name in profiles:
        compile_job = jobs.get((name, 'compile'))
        if selected not in ('all', name) or not compile_job or compile_job['conclusion'] != 'success':
            continue
        has_successful_build = True
        attempt = str(compile_job['run_attempt'])
        candidates = [artifact for artifact in available
                      if artifact['name'] == 'firmware-' + name + '-' + attempt]
        if not candidates:
            continue
        artifact = max(candidates, key=lambda item: int(item['id']))
        matrix['include'].append(dict(profile=name, source_run_id=str(run['id']), source_run_attempt=attempt,
                                      artifact_id=str(artifact['id'])))
    if has_successful_build and not matrix['include']:
        raise ValueError('No successful, unexpired firmware artifacts for this selection')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix) + '\n')
        output.write('has_releases=' + str(bool(matrix['include'])).lower() + '\n')
    print(json.dumps(matrix))


def validate_info(info, run, profile, attempt=None):
    if (info['profile_id'] != profile or str(info['run_id']) != str(run['id'])
            or info['ci_commit'] != run['head_sha']
            or not re.fullmatch(r'[0-9a-f]{40}', info['ci_commit'])
            or not re.fullmatch(r'[0-9a-f]{64}', info['fingerprint'])):
        raise ValueError('Artifact provenance does not match its source build')
    if attempt is not None and 'run_attempt' in info and str(info['run_attempt']) != attempt:
        raise ValueError('Artifact belongs to a different compilation attempt')


def release_assets(directory, info):
    names = info.get('release_assets')
    if names is None:
        # Keep legacy artifact layouts valid when rerunning older producer jobs.
        return sorted(p for p in directory.iterdir() if p.is_file())
    if (not isinstance(names, list) or not all(isinstance(name, str) for name in names)
            or len(names) != len(set(names))
            or not {'build-info.json', 'sha256sums'}.issubset(names)):
        raise ValueError('Invalid release asset list')
    for name in names:
        if name in ('.', '..', 'release-notes.md') or Path(name).name != name or '\\' in name:
            raise ValueError('Unsafe release asset name')
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Release asset missing: ' + name)
    checksum_names = []
    for line in (directory / 'sha256sums').read_text().splitlines():
        match = re.fullmatch(r'[0-9a-f]{64}  (.+)', line)
        if not match:
            raise ValueError('Invalid checksum entry')
        checksum_names.append(match[1])
    if set(checksum_names) != set(names) - {'sha256sums'} or len(checksum_names) != len(set(checksum_names)):
        raise ValueError('Checksums do not cover exactly the public release assets')
    return sorted(directory / name for name in names)


def publish(directory):
    repository, repo, run = context()
    profile = os.environ['PROFILE']
    profiles = read_toml(ROOT / 'Config/profiles.toml')['profiles']
    attempt = os.environ['SOURCE_RUN_ATTEMPT']
    if profile not in profiles:
        raise ValueError('Invalid profile')
    verify_build(repository, run, profile, attempt)
    directory = directory.resolve()
    info = json.loads((directory / 'build-info.json').read_text())
    validate_info(info, run, profile, attempt)
    assets = release_assets(directory, info)
    subprocess.run(['sha256sum', '-c', 'sha256sums'], cwd=directory, check=True)
    marker = '<!-- build-fingerprint:' + info['fingerprint'] + ' -->'
    notes = (directory / 'release-notes.md').read_text()
    if marker not in notes:
        raise ValueError('Release notes fingerprint does not match build metadata')
    notes += '\n<details>\n<summary>构建追溯</summary>\n\n'
    notes += 'CI：[`' + info['ci_commit'][:7] + '`](https://github.com/' + repository + '/commit/' + info['ci_commit'] + ')'
    notes += ' · [Actions #' + str(run['id']) + '](https://github.com/' + repository + '/actions/runs/' + str(run['id']) + ')。\n\n'
    notes += '完整构建记录见 `build-info.json`；Release 标签仅用于发布索引。\n\n</details>\n'
    tag = profile + '-r' + str(run['id']) + '-' + attempt
    existing = subprocess.run(['gh', 'api', 'repos/' + repository + '/releases/tags/' + tag],
                              capture_output=True, text=True)
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
                            '--target', repo['default_branch'], '--title', profiles[profile].get('display_name', profile) + ' · ' + info['built_at'][:10],
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
