"""Unified build workflow and per-profile publication regressions."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from Scripts import build, release


class UnifiedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (build.ROOT / '.github/workflows/build.yml').read_text()
        blocks = re.split(r'(?m)^  ([a-z][a-z0-9-]*):\n', self.workflow.split('jobs:\n', 1)[1])
        self.jobs = dict(zip(blocks[1::2], blocks[2::2]))

    def test_only_build_cleanup_and_checks_have_workflow_files(self):
        self.assertEqual({path.name for path in (build.ROOT / '.github/workflows').glob('*.yml')},
                         {'build.yml', 'cleanup.yml', 'check.yml'})
        self.assertEqual(set(self.jobs), {'plan', 'compile', 'publish-plan', 'publish'})
        self.assertNotIn('uses: ./.github/workflows/', self.workflow)
        self.assertNotIn('workflow_call:', self.workflow)
        self.assertNotIn('workflow_run:', self.workflow)
        self.assertNotIn('AUTO_RECOVER', self.workflow)

    def test_release_permissions_are_isolated_from_compilation(self):
        self.assertEqual(self.workflow.count('contents: write'), 1)
        for name in ('compile', 'publish-plan'):
            header = self.jobs[name].split('    steps:\n', 1)[0]
            with self.subTest(job=name):
                self.assertIn('contents: read', header)
                self.assertIn('actions: read', header)
                self.assertNotIn('contents: write', header)
        self.assertIn('contents: write', self.jobs['publish'].split('    steps:\n', 1)[0])

    def test_partial_build_failure_does_not_block_successful_publications(self):
        selection = self.jobs['publish-plan'].split('    steps:\n', 1)[0]
        self.assertIn('needs: [plan, compile]', selection)
        self.assertIn('!cancelled()', selection)
        self.assertIn("needs.plan.result == 'success'", selection)
        self.assertIn("needs.plan.outputs.has_builds == 'true'", selection)
        self.assertIn("github.event_name == 'schedule' || inputs.publish == true", selection)
        self.assertNotIn('needs.compile.result', selection)
        publisher = self.jobs['publish'].split('    steps:\n', 1)[0]
        self.assertIn('!cancelled()', publisher)
        self.assertIn("needs.publish-plan.result == 'success'", publisher)
        self.assertIn("needs.publish-plan.outputs.has_releases == 'true'", publisher)
        self.assertIn('fail-fast: false', self.jobs['compile'])
        self.assertIn('fail-fast: false', publisher)
        self.assertNotIn('continue-on-error: true', self.jobs['compile'].split('    steps:\n', 1)[0])

    def test_artifacts_keep_each_producers_identity_instead_of_matrix_outputs(self):
        self.assertIn('name: ${{ matrix.profile }} / compile', self.jobs['compile'])
        self.assertIn('name: ${{ matrix.profile }} / publish', self.jobs['publish'])
        self.assertIn('group: firmware-${{ matrix.profile }}', self.jobs['compile'])
        self.assertIn('artifact-ids: ${{ needs.plan.outputs.plan_artifact_id }}', self.jobs['compile'])
        self.assertIn('name: firmware-${{ matrix.profile }}-${{ github.run_attempt }}', self.jobs['compile'])
        self.assertNotIn('needs.compile.outputs', self.workflow)
        self.assertIn('SOURCE_RUN_ID: ${{ github.run_id }}', self.jobs['publish-plan'])
        self.assertIn('matrix: ${{ fromJSON(needs.publish-plan.outputs.matrix) }}', self.jobs['publish'])
        self.assertIn('artifact-ids: ${{ matrix.artifact_id }}', self.jobs['publish'])
        self.assertIn('run-id: ${{ matrix.source_run_id }}', self.jobs['publish'])
        self.assertIn('SOURCE_RUN_ATTEMPT: ${{ matrix.source_run_attempt }}', self.jobs['publish'])


class ReleasePlanningTests(unittest.TestCase):
    profiles = {'s20p-mt76': {}, 'x86-64': {}}

    def job(self, profile, number, attempt=1, conclusion='success', kind='compile'):
        return dict(id=number, name=profile + ' / ' + kind, run_attempt=attempt, conclusion=conclusion)

    def artifact(self, profile, number, attempt=1, expired=False):
        return dict(id=number, name=f'firmware-{profile}-{attempt}', expired=expired)

    def plan(self, jobs, artifacts, profile='all'):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output.txt'
            with patch.object(release, 'context', return_value=('owner/repo', {}, {'id': 123, 'run_attempt': 2})), \
                    patch.object(release, 'read_toml', return_value={'profiles': self.profiles}), \
                    patch.object(release, 'gh_json', side_effect=[[{'jobs': jobs}], [{'artifacts': artifacts}]]), \
                    patch.dict(release.os.environ, {'PROFILE': profile, 'GITHUB_OUTPUT': str(output)}, clear=True), \
                    patch('builtins.print'):
                release.plan()
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            matrix = json.loads(values['matrix'])['include']
            self.assertEqual(values['has_releases'], str(bool(matrix)).lower())
            return matrix

    def test_successful_compilation_is_published_without_a_previous_publisher(self):
        matrix = self.plan([self.job('s20p-mt76', 1)], [self.artifact('s20p-mt76', 101)])
        self.assertEqual(matrix, [dict(profile='s20p-mt76', source_run_id='123',
                                      source_run_attempt='1', artifact_id='101')])

    def test_failed_or_cancelled_profiles_do_not_hide_successful_profiles(self):
        for conclusion in ('failure', 'timed_out', 'cancelled', 'skipped', None):
            with self.subTest(conclusion=conclusion):
                jobs = [self.job('s20p-mt76', 1, conclusion=conclusion), self.job('x86-64', 2)]
                artifacts = [self.artifact('s20p-mt76', 101), self.artifact('x86-64', 102)]
                matrix = self.plan(jobs, artifacts)
                self.assertEqual([item['profile'] for item in matrix], ['x86-64'])

    def test_all_failed_compilations_produce_an_empty_publication_matrix(self):
        self.assertEqual(self.plan([self.job('s20p-mt76', 1, conclusion='failure')], []), [])
        self.assertEqual(self.plan([], []), [])

    def test_successful_compilation_without_an_available_artifact_is_visible(self):
        for artifacts in ([], [self.artifact('s20p-mt76', 101, expired=True)]):
            with self.subTest(artifacts=artifacts), self.assertRaisesRegex(ValueError, 'unexpired firmware artifacts'):
                self.plan([self.job('s20p-mt76', 1)], artifacts)

    def test_partial_rerun_keeps_successful_artifacts_from_their_original_attempt(self):
        jobs = [self.job('s20p-mt76', 1), self.job('x86-64', 2, conclusion='failure'),
                self.job('x86-64', 3, attempt=2)]
        artifacts = [self.artifact('s20p-mt76', 101), self.artifact('x86-64', 102),
                     self.artifact('x86-64', 103, attempt=2)]
        matrix = self.plan(jobs, artifacts)
        self.assertEqual([(item['profile'], item['source_run_attempt'], item['artifact_id']) for item in matrix],
                         [('s20p-mt76', '1', '101'), ('x86-64', '2', '103')])

    def test_latest_failed_attempt_cannot_fall_back_to_a_stale_success(self):
        jobs = [self.job('s20p-mt76', 1), self.job('s20p-mt76', 2, attempt=2, conclusion='failure')]
        self.assertEqual(self.plan(jobs, [self.artifact('s20p-mt76', 101)]), [])

    def test_artifact_selection_ignores_expired_and_other_profiles(self):
        jobs = [self.job('s20p-mt76', 1), self.job('x86-64', 2)]
        artifacts = [self.artifact('s20p-mt76', 101), self.artifact('s20p-mt76', 103, expired=True),
                     self.artifact('s20p-mt76', 102), self.artifact('x86-64', 104),
                     dict(id=999, name='logs-s20p-mt76-1', expired=False)]
        self.assertEqual(self.plan(jobs, artifacts, 's20p-mt76'),
                         [dict(profile='s20p-mt76', source_run_id='123', source_run_attempt='1', artifact_id='102')])

    def test_job_lookup_handles_current_and_nested_names_across_pages(self):
        pages = [{'jobs': [self.job('s20p-mt76', 1), self.job('x86-64', 2)]},
                 {'jobs': [self.job('s20p-mt76', 3, attempt=2),
                           dict(self.job('x86-64', 4), name='x86-64 / publish / publish'),
                           dict(self.job('ignored', 5), name='Select successful firmware')]}]
        with patch.object(release, 'gh_json', return_value=pages):
            latest = release.latest_jobs('owner/repo', {'id': 123})
        self.assertEqual({key: job['id'] for key, job in latest.items()},
                         {('s20p-mt76', 'compile'): 3, ('x86-64', 'compile'): 2, ('x86-64', 'publish'): 4})

    def test_publication_requires_the_successful_producer_attempt(self):
        run = {'id': 123, 'run_attempt': 2}
        for attempt, conclusion, valid in (('1', 'success', True), ('2', 'success', False),
                                           ('1', 'failure', False), ('0', 'success', False)):
            jobs = {('s20p-mt76', 'compile'): self.job('s20p-mt76', 1, conclusion=conclusion)}
            with self.subTest(attempt=attempt, conclusion=conclusion), \
                    patch.object(release, 'latest_jobs', return_value=jobs):
                if valid:
                    release.verify_build('owner/repo', run, 's20p-mt76', attempt)
                else:
                    with self.assertRaises(ValueError):
                        release.verify_build('owner/repo', run, 's20p-mt76', attempt)


if __name__ == '__main__':
    unittest.main()
