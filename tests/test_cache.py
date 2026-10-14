"""Shared compiler caches, stable installed caches and replacement retention."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Scripts import build, cleanup
from test_build import fixture


class CacheWriterTests(unittest.TestCase):
    def setUp(self):
        self.profiles = build.read_toml(build.ROOT / 'Config/profiles.toml')['profiles']

    def matrix(self, names):
        matrix = [dict(profile=name) for name in names]
        build.assign_cache_writers(matrix, self.profiles)
        return matrix

    def test_all_profiles_have_five_writers_and_one_shared_filogic_writer(self):
        matrix = self.matrix(self.profiles)
        self.assertEqual(sum(item['ccache_write'] for item in matrix), 5)
        shared = [item for item in matrix if item['ccache_group'] == 'viking-filogic']
        self.assertEqual({item['profile'] for item in shared},
                         {'7dr7299-mt76', 's20p-mt76', 'tenda-be12-pro'})
        self.assertEqual([item['profile'] for item in shared if item['ccache_write']], ['7dr7299-mt76'])

    def test_selected_profiles_choose_an_available_writer(self):
        for names, expected in ((['tenda-be12-pro'], 'tenda-be12-pro'),
                                (['tenda-be12-pro', 's20p-mt76'], 's20p-mt76')):
            with self.subTest(names=names):
                self.assertEqual([item['profile'] for item in self.matrix(names) if item['ccache_write']], [expected])
        self.assertEqual(self.matrix([]), [])

    def test_groups_cannot_mix_sources_platforms_or_unsafe_names(self):
        original = deepcopy(self.profiles)
        for key, value in (('source', 'chasey'), ('subtarget', 'other'), ('ccache_group', '../bad'),
                           ('ccache_priority', True), ('ccache_priority', -1)):
            self.profiles = deepcopy(original)
            self.profiles['s20p-mt76'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.matrix(self.profiles)


class InstalledCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / 'openwrt'
        self.config = 'CONFIG_TARGET_mediatek=y\nCONFIG_GCC_VERSION="14.3"\nCONFIG_PACKAGE_luci=y\n'
        fixture(self.tree, '.config', self.config)
        self.tracked = ['tools/Makefile', 'toolchain/gcc/Makefile', 'include/host.mk',
                        'target/linux/mediatek/filogic/config-6.18']
        for name in self.tracked:
            fixture(self.tree, name, '# CONFIG_GCC_VERSION\n')
        fixture(self.root, 'Scripts/install-dependencies.sh', 'apt-get install gcc\n')
        fixture(self.root, 'Scripts/build.py', '# recipes\n')
        self.lock = dict(profile_id='s20p-mt76', profile={'target': 'mediatek'}, config={}, ci_hash='old-ci',
                         source=dict(repository='https://example.test/source', branch='main', commit='a' * 40,
                                     feeds=[{'name': 'packages', 'commit': 'b' * 40}]),
                         packages={'plugin': {'commit': 'c' * 40}})
        self.host = dict(image='ubuntu24-image', compiler='gcc14', linker='ld', packages='libc6=1')
        self.root_patch = patch.object(build, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def git(self, *args, **kwargs):
        if args[:2] == ('git', 'ls-files'):
            return '\0'.join('100644 ' + 'a' * 40 + ' 0\t' + name for name in self.tracked) + '\0'
        if args == ('git', 'rev-parse', 'HEAD:package'):
            return 'd' * 40
        raise AssertionError('Unexpected command: ' + repr(args))

    def keys(self):
        with patch.object(build, 'run', side_effect=self.git):
            return build.cache_keys(self.lock, self.tree, self.host)

    def test_keys_survive_new_runs_unrelated_commits_readme_and_other_model_edits(self):
        first = self.keys()
        self.lock['source']['commit'] = 'e' * 40
        self.lock['ci_hash'] = 'new-ci'
        fixture(self.root, 'README.md', 'documentation changed')
        fixture(self.root, 'Config/other.txt', 'CONFIG_PACKAGE_other=y')
        with patch.dict(build.os.environ, {'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '2'}):
            self.assertEqual(first, self.keys())
        self.assertRegex(first['toolchain_key'], r'^toolchain-v2-Linux-ubuntu24-s20p-mt76-[0-9a-f]{24}$')
        self.assertRegex(first['hostpkg_key'], r'^hostpkg-v2-Linux-ubuntu24-s20p-mt76-[0-9a-f]{24}$')

    def test_expanded_compiler_bpf_and_host_changes_invalidate_installed_caches(self):
        original = self.keys()
        for option in ('CONFIG_GCC_VERSION="15"', 'CONFIG_CPU_TYPE="cortex-a73"', 'CONFIG_BPF_TOOLCHAIN_HOST=y'):
            with self.subTest(option=option):
                fixture(self.tree, '.config', self.config + option + '\n')
                changed = self.keys()
                self.assertNotEqual(original['toolchain_key'], changed['toolchain_key'])
                self.assertNotEqual(original['hostpkg_key'], changed['hostpkg_key'])
        fixture(self.tree, '.config', self.config)
        self.host['packages'] = 'libc6=2'
        self.assertNotEqual(original['toolchain_key'], self.keys()['toolchain_key'])

    def test_local_tracked_toolchain_and_kernel_patches_invalidate_caches(self):
        for name in self.tracked:
            original = self.keys()
            fixture(self.tree, name, '# patched locally\n')
            with self.subTest(path=name):
                self.assertNotEqual(original['toolchain_key'], self.keys()['toolchain_key'])

    def test_feed_and_overlay_changes_invalidate_only_package_host_tools(self):
        for kind in ('feed', 'overlay', 'script'):
            original = self.keys()
            if kind == 'feed':
                self.lock['source']['feeds'][0]['commit'] = 'f' * 40
            elif kind == 'overlay':
                self.lock['packages']['plugin']['commit'] = 'e' * 40
            else:
                fixture(self.root, 'Scripts/build.py', '# changed integration recipe\n')
            changed = self.keys()
            with self.subTest(kind=kind):
                self.assertEqual(original['toolchain_key'], changed['toolchain_key'])
                self.assertNotEqual(original['hostpkg_key'], changed['hostpkg_key'])

    def test_package_selection_keeps_foundation_unless_its_rules_reference_it(self):
        original = self.keys()
        fixture(self.tree, '.config', self.config + 'CONFIG_PACKAGE_extra=y\n')
        changed = self.keys()
        self.assertEqual(original['toolchain_key'], changed['toolchain_key'])
        self.assertNotEqual(original['hostpkg_key'], changed['hostpkg_key'])
        for reference in ('CONFIG_PACKAGE_extra', 'CONFIG_PACKAGE_$(name)'):
            fixture(self.tree, 'tools/Makefile', reference)
            original = self.keys()
            fixture(self.tree, '.config', self.config)
            with self.subTest(reference=reference):
                self.assertNotEqual(original['toolchain_key'], self.keys()['toolchain_key'])
            fixture(self.tree, '.config', self.config + 'CONFIG_PACKAGE_extra=y\n')

    def test_generated_files_and_python_bytecode_are_not_inputs(self):
        original = self.keys()
        fixture(self.tree, 'scripts/config/conf', 'generated executable')
        fixture(self.tree, 'scripts/config/parser.o', 'generated object')
        fixture(self.root, 'Scripts/__pycache__/build.cpython-312.pyc', 'bytecode')
        self.assertEqual(original, self.keys())

    def test_unprepared_source_is_rejected(self):
        fixture(self.tree, '.config', '')
        with self.assertRaisesRegex(ValueError, 'expanded'):
            self.keys()

    def test_cache_plan_emits_keys_and_diagnostics_for_enabled_caches(self):
        build.write_json(self.root / 'lock.json', self.lock)

        def run(*args, **kwargs):
            if args[0] in ('gcc', 'ld', 'dpkg-query'):
                return 'host environment'
            return self.git(*args, **kwargs)

        output = self.root / 'output.txt'
        with patch.object(build, 'run', side_effect=run):
            build.cache_plan(SimpleNamespace(lock=self.root / 'lock.json', source=self.tree,
                                            github_output=output))
        result = dict(line.split('=', 1) for line in output.read_text().splitlines())
        self.assertEqual(result['enabled'], 'true')
        self.assertRegex(result['toolchain_key'], r'^toolchain-v2-Linux-ubuntu24-s20p-mt76-[0-9a-f]{24}$')
        self.assertRegex(result['hostpkg_key'], r'^hostpkg-v2-Linux-ubuntu24-s20p-mt76-[0-9a-f]{24}$')
        self.assertEqual(result, build.read_json(self.root / 'logs/cache-plan.json'))

    def test_private_hook_disables_installed_cache_restore_and_save(self):
        fixture(self.root, 'Scripts/PRIVATE.sh', '# arbitrary trusted changes\n')
        build.write_json(self.root / 'lock.json', self.lock)
        with patch.object(build, 'run') as run:
            build.cache_plan(SimpleNamespace(lock=self.root / 'lock.json', source=self.tree,
                                            github_output=self.root / 'output.txt'))
            run.assert_not_called()
        self.assertIn('enabled=false', (self.root / 'output.txt').read_text())
        self.assertEqual(build.read_json(self.root / 'logs/cache-plan.json')['toolchain_key'], '')


class CacheWorkflowTests(unittest.TestCase):
    def test_restore_follows_defconfig_and_saves_follow_firmware_artifact(self):
        workflow = (build.ROOT / '.github/workflows/build.yml').read_text()
        self.assertLess(workflow.index('name: Integrate plugins'), workflow.index('id: cache-plan'))
        self.assertLess(workflow.index('id: cache-plan'), workflow.index('id: toolchain-cache'))
        self.assertLess(workflow.index('id: firmware-artifact'), workflow.index('name: Save successful compiler cache'))
        self.assertIn('CCACHE_COMPILERCHECK: content', workflow)
        self.assertIn('if: matrix.ccache_write', workflow)
        for kind in ('toolchain', 'hostpkg'):
            restore = workflow.split('        id: ' + kind + '-cache\n', 1)[1].split('      - ', 1)[0]
            self.assertNotIn('restore-keys:', restore)
            self.assertIn("steps.cache-plan.outputs.enabled == 'true'", restore)
            self.assertIn("steps." + kind + "-cache.outputs.cache-hit != 'true'", workflow)


class CacheRetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        self.profiles = build.read_toml(build.ROOT / 'Config/profiles.toml')['profiles']
        self.values = cleanup.settings()

    def cache(self, number, key, ref='refs/heads/main'):
        return dict(id=number, key=key, ref=ref, size_in_bytes=100,
                    created_at=(self.now - timedelta(minutes=10 - number)).isoformat(),
                    last_accessed_at=self.now.isoformat())

    def test_one_generation_replaces_recent_caches_without_a_day_of_duplicates(self):
        self.assertEqual(self.values['cache_keep_generations'], 1)
        self.assertEqual(self.values['cache_min_age_days'], 0)
        entries = [self.cache(n, f'ccache-v2-Linux-ubuntu24-viking-filogic-{n}-1') for n in (1, 2)]
        entries += [self.cache(3, 'ccache-v2-Linux-ubuntu24-viking-filogic-3-1', 'refs/heads/test'),
                    self.cache(4, 'unknown-key')]
        self.assertEqual([item['id'] for item in cleanup.cache_candidates(entries, self.profiles, self.values, self.now)], [1])

    def test_failed_or_empty_replacement_cannot_remove_the_only_cache(self):
        old = self.cache(1, 'ccache-v2-Linux-ubuntu24-viking-filogic-1-1')
        incomplete = dict(self.cache(2, 'ccache-v2-Linux-ubuntu24-viking-filogic-2-1'), size_in_bytes=0)
        self.assertEqual(cleanup.cache_candidates([old, incomplete], self.profiles, self.values, self.now), [])

    def test_installed_kinds_are_separate_and_keep_the_most_recently_used_compatible_key(self):
        entries = []
        for kind in ('toolchain', 'hostpkg'):
            for n in (1, 2):
                entry = self.cache(len(entries) + 1, f'{kind}-v2-Linux-ubuntu24-s20p-mt76-' + str(n) * 24)
                if n == 2:
                    entry['last_accessed_at'] = (self.now - timedelta(hours=1)).isoformat()
                entries.append(entry)
        self.assertEqual({item['id'] for item in cleanup.cache_candidates(entries, self.profiles, self.values, self.now)}, {2, 4})


if __name__ == '__main__':
    unittest.main()
