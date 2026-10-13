"""Regression tests for build customization, caching and conservative publication."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Scripts import build, cleanup, release


def fixture(tree, name, content):
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class RepositoryLayoutTests(unittest.TestCase):
    def test_config_directory_has_standalone_models_and_toml_settings(self):
        config_dir = build.ROOT / 'Config'
        profiles = build.read_toml(config_dir / 'profiles.toml')['profiles']
        paths = [build.ROOT / profile['config'] for profile in profiles.values()]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), {path for path in config_dir.glob('*.txt') if path.name != 'PRIVATE.txt'})
        self.assertEqual({path.name for path in config_dir.glob('*.toml')},
                         {'profiles.toml', 'packages.toml', 'firmware-defaults.toml', 'build-settings.toml'})
        self.assertEqual(list(config_dir.glob('*.json')), [])
        for path in paths:
            with self.subTest(config=path.name):
                lines = [line.strip() for line in path.read_text().splitlines()
                         if line.strip() and not line.lstrip().startswith('#')]
                for line in lines:
                    self.assertRegex(line, r'^CONFIG_[^=]+=[^\r\n]+$')
                keys = [line.split('=', 1)[0] for line in lines]
                self.assertEqual(len(keys), len(set(keys)), 'Duplicate configuration option')
                self.assertTrue(build.merge_config(['\n'.join(lines)]))

    def test_config_comments_are_short_and_chinese(self):
        for path in (build.ROOT / 'Config').iterdir():
            if path.suffix not in ('.txt', '.toml') or path.name == 'PRIVATE.txt':
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith('#'):
                    with self.subTest(file=path.name, line=number):
                        self.assertRegex(line, r'[\u4e00-\u9fff]')
                        self.assertLessEqual(len(line), 60)

    def test_toml_comments_and_value_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = fixture(Path(directory), 'settings.toml',
                           '# 构建设置\nscheduled_builds = true # 启用\nmax_parallel = 5\n'
                           '[driver]\n' + "WARP_VERSION = '\"3\"'\n")
            self.assertEqual(build.read_toml(path),
                             {'scheduled_builds': True, 'max_parallel': 5, 'driver': {'WARP_VERSION': '"3"'}})
            path.write_text('max_parallel = 1\nmax_parallel = 2\n')
            with self.assertRaises(ValueError):
                build.read_toml(path)

    def test_plan_loads_all_profiles_from_config(self):
        profiles = build.read_toml(build.ROOT / 'Config/profiles.toml')['profiles']
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            args = SimpleNamespace(profile='all', output_dir=output, event='workflow_dispatch',
                                   force=True, commit=None, packages='', github_output=None)
            with patch.object(build, 'resolve', return_value='a' * 40), \
                    patch.object(build, 'request', return_value=b'src-git packages https://example.test/packages.git;main\n'), \
                    patch.object(build, 'xray_lock', return_value={'version': 'test'}), \
                    patch.object(build, 'ci_hash', return_value='b' * 64), \
                    patch.object(build, 'run', side_effect=AssertionError('Unexpected external command')), \
                    patch.dict(build.os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}):
                build.plan(args)
            matrix = build.read_json(output / 'matrix.json')
            self.assertEqual({item['profile'] for item in matrix['matrix']['include']}, set(profiles))
            for name, profile in profiles.items():
                with self.subTest(profile=name):
                    lock = build.read_json(output / (name + '.json'))
                    self.assertEqual(lock['config'], build.config_for(profile))
                    self.assertEqual(lock['defaults'], build.defaults(name))
                    self.assertNotIn('fragments', profile)
                    config = build.ROOT / profile['config']
                    self.assertEqual(config.parent, build.ROOT / 'Config')
                    self.assertTrue(config.is_file())

    def test_integrate_loads_hooks_and_recipe_assets_from_scripts(self):
        lock = dict(profile_id='s20p-mt76', profile={'legacy_name': 'SUPERGATEWAY-S20P-MT76'},
                    packages={name: {'mode': name, 'commit': 'a' * 40} for name in ('mesh', 'honk')},
                    xray=dict(version='1.2.3', tag='v1.2.3', asset='Xray-linux-arm64-v8a.zip',
                              asset_id=123, sha256='b' * 64))
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / 'package').mkdir()
            with patch.object(build, 'run') as run:
                build.integrate(lock, tree)
            self.assertEqual([Path(call.args[1]) for call in run.call_args_list],
                             [build.ROOT / 'Scripts/Mesh.sh', build.ROOT / 'Scripts/Honk.sh'])
            for call in run.call_args_list:
                self.assertTrue(Path(call.args[1]).is_file())
                self.assertEqual(call.kwargs['cwd'], tree)
            for name in ('upx-arm64-static', 'owrt-core-update'):
                self.assertEqual((tree / 'package' / name / 'Makefile').read_bytes(),
                                 (build.ROOT / 'Scripts/Makefiles' / (name + '.mk')).read_bytes())
            self.assertEqual((tree / 'package/owrt-core-update/files/owrt-core-update').read_bytes(),
                             (build.ROOT / 'Scripts/Files/core-update/owrt-core-update').read_bytes())
            recipe = (tree / 'package/xray-core/Makefile').read_text()
            self.assertIn('b' * 64, recipe)
            self.assertNotIn('@XRAY_', recipe)

    def test_recovery_loads_profile_registry_from_config(self):
        with patch.object(release, 'context', return_value=('owner/repo', {}, {'id': 123})), \
                patch.dict(release.os.environ, {'PROFILE': 'not-a-profile'}):
            with self.assertRaisesRegex(ValueError, 'Unknown profile'):
                release.plan()


class CustomizationTests(unittest.TestCase):
    def test_package_input_is_config_not_shell_or_platform(self):
        values = build.merge_config([build.package_overrides('CONFIG_PACKAGE_samba4=y\\nCONFIG_PACKAGE_curl=n')])
        self.assertEqual(values, {'PACKAGE_samba4': 'y', 'PACKAGE_curl': 'n'})
        for content in ('$(touch nope)', 'CONFIG_TARGET_x86=y', '# CONFIG_TARGET_x86 is not set', 'CONFIG_PACKAGE_curl=y; echo nope'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                build.package_overrides(content)

    def test_private_and_temporary_merge_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(build, 'ROOT', Path(directory)):
            root = Path(directory)
            fixture(root, 'Config/DEVICE.txt', 'CONFIG_PACKAGE_curl=y\nCONFIG_PACKAGE_samba4=m\n')
            profile = {'config': 'Config/DEVICE.txt'}
            self.assertEqual(build.config_for(profile), {'PACKAGE_curl': 'y', 'PACKAGE_samba4': 'm'})
            fixture(root, 'Config/PRIVATE.txt', 'CONFIG_PACKAGE_curl=n\nCONFIG_PACKAGE_samba4=y\n')
            self.assertEqual(build.config_for(profile), {'PACKAGE_curl': 'n', 'PACKAGE_samba4': 'y'})
            self.assertEqual(build.config_for(profile, 'CONFIG_PACKAGE_curl=m'),
                             {'PACKAGE_curl': 'm', 'PACKAGE_samba4': 'y'})

    def test_temporary_configuration_has_a_distinct_schedule_marker(self):
        baseline = {'PACKAGE_curl': 'y'}
        self.assertNotEqual(build.config_marker(baseline), build.config_marker({'PACKAGE_curl': 'n'}))
        self.assertEqual(build.config_marker(baseline), build.config_marker(dict(baseline)))

    def test_luci_identity_ip_and_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            status = fixture(tree, 'feeds/luci/modules/luci-mod-status/view/10_system.js', "var x = (luciversion || '');\n")
            flash = fixture(tree, 'feeds/luci/modules/luci-mod-system/view/flash.js',
                            "keep ? ui.awaitReconnect(window.location.host) : ui.awaitReconnect('192.168.1.1', 'immortalwrt.lan');\n")
            collection = fixture(tree, 'feeds/luci/collections/luci/Makefile',
                                 'LUCI_DEPENDS:=+luci-base \\\n+ +PACKAGE_luci:luci-app-attendedsysupgrade \\\n+ +luci-mod-system +luci-app-attendedsysupgrade\n# attendedsysupgrade description stays\n')
            build.customize_luci(tree, {'lan_ip': '192.168.123.1'}, {'repository_owner': 'WoChen5770', 'build_date': '26.10.12-09.00.00'})
            self.assertIn('WoChen5770-26.10.12-09.00.00', status.read_text())
            self.assertIn("'192.168.123.1'", flash.read_text())
            self.assertIn('ui.awaitReconnect(window.location.host)', flash.read_text())
            self.assertIn('+luci-base \\\n', collection.read_text())
            self.assertIn('+luci-mod-system', collection.read_text())
            self.assertNotIn('+luci-app-attendedsysupgrade', collection.read_text())
            self.assertNotIn('+PACKAGE_luci:', collection.read_text())
            self.assertIn('# attendedsysupgrade description stays', collection.read_text())

    def test_luci_upstream_change_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            fixture(tree, 'feeds/luci/modules/luci-mod-status/10_system.js', 'changed anchor')
            fixture(tree, 'feeds/luci/modules/luci-mod-system/flash.js', "'192.168.1.1'")
            with self.assertRaisesRegex(ValueError, 'anchor changed'):
                build.customize_luci(tree, {'lan_ip': '192.168.123.1'}, {'repository_owner': 'owner', 'build_date': 'date'})

    def test_qmodem_links_and_nested_recipes_removed_without_following_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree, external = root / 'source', root / 'external'
            fixture(tree, 'feeds/custom/deep/luci-app-qmodem-next/Makefile', 'bad')
            fixture(tree, 'package/qmodem-generic/Makefile', 'bad')
            keep = fixture(tree, 'feeds/custom/not-qmodem/Makefile', 'good')
            outside = fixture(external, 'qmodem/Makefile', 'outside')
            (tree / 'package/luci-app-qmodem').symlink_to(external / 'qmodem', target_is_directory=True)
            (tree / 'package/other').symlink_to(external, target_is_directory=True)
            (tree / 'package/qmodem-broken').symlink_to(root / 'missing')
            build.remove_qmodem(tree)
            self.assertTrue(keep.is_file())
            self.assertTrue(outside.is_file())
            self.assertFalse((tree / 'package/luci-app-qmodem').is_symlink())
            self.assertFalse((tree / 'package/qmodem-broken').is_symlink())
            self.assertFalse((tree / 'feeds/custom/deep/luci-app-qmodem-next').exists())

    def test_cache_refresh_never_touches_target(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            host = fixture(tree, 'staging_dir/host/stamp/.tools_compile', 'done')
            target = fixture(tree, 'staging_dir/target-test/stamp/.target_compile', 'done')
            before = target.stat().st_mtime_ns
            build.refresh_toolchain_cache(tree)
            self.assertEqual(before, target.stat().st_mtime_ns)
            self.assertTrue(host.is_file())
            self.assertEqual((tree / 'tmp/.build').read_text(), '1\n')

    def test_cache_compatibility_changes_with_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = dict(source=dict(repository='https://github.com/owner/repo', branch='main', feeds=[], commit='a' * 40),
                        profile_id='x86-64', config={'TARGET_x86': 'y'}, packages={}, ci_hash='b' * 64)
            build.write_json(root / 'lock.json', lock)
            def cache_key():
                output = root / 'output.txt'
                output.write_text('')
                build.cache_plan(SimpleNamespace(lock=root / 'lock.json', source=root, github_output=output))
                return output.read_text().splitlines()[0]
            with patch.object(build, 'run', return_value='c' * 40), patch.dict(build.os.environ, {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}):
                original = cache_key()
                lock['source']['commit'] = 'd' * 40
                build.write_json(root / 'lock.json', lock)
                self.assertEqual(original, cache_key())
                lock['config']['TOOLCHAINOPTS'] = 'y'
                build.write_json(root / 'lock.json', lock)
                self.assertNotEqual(original, cache_key())

    def test_prepare_runs_private_hook_before_pruning_and_rejects_dropped_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / 'openwrt'
            tree.mkdir()
            fixture(root, 'Scripts/PRIVATE.sh', '#!/bin/bash\n')
            lock = dict(ci_hash='locked', repository_owner='owner', profile_id='x86-64',
                        profile=dict(legacy_name='X86', target='x86', subtarget='64', source='viking'),
                        source=dict(commit='a' * 40, feeds=[], branch='main'), defaults={'lan_ip': '192.168.123.1'},
                        config={'PACKAGE_example': 'y'}, requested_packages={'PACKAGE_example': 'y'})
            build.write_json(root / 'lock.json', lock)
            def fake_run(*args, cwd=None, env=None):
                if args[0] == 'git':
                    return 'a' * 40
                if args[0] == 'bash':
                    self.assertEqual(cwd, tree.resolve())
                    self.assertEqual(env['WRT_MARK'], 'owner')
                    self.assertNotIn('GH_TOKEN', env)
                    fixture(tree, 'package/luci-app-qmodem/Makefile', 'added by hook')
                if args[:2] == ('make', 'defconfig'):
                    (tree / '.config').write_text('')
                return ''
            def check_defaults(*args):
                self.assertFalse((tree / 'package/luci-app-qmodem').exists())
            with patch.object(build, 'ROOT', root), patch.object(build, 'ci_hash', return_value='locked'), \
                    patch.object(build, 'run', side_effect=fake_run), patch.object(build, 'integrate'), \
                    patch.object(build, 'apply_defaults', side_effect=check_defaults), \
                    patch.object(build, 'customize_luci'), patch.object(build, 'validate_config'):
                with self.assertRaisesRegex(ValueError, 'Private/temporary options not selected: PACKAGE_example'):
                    build.prepare(SimpleNamespace(lock=root / 'lock.json', source=tree, toolchain_cache=False))

    def test_firmware_extensions_preserved(self):
        for name in ('immortalwrt-25.12-generic.img.gz', 'firmware.bin', 'disk.vmdk', 'sysupgrade.tar.gz'):
            with self.subTest(name=name):
                result = build.dated_filename(name, 'x86-64-26.10.12-09.00.00-r123-1')
                self.assertIn('x86-64-26.10.12-09.00.00', result)
                self.assertTrue(result.endswith('.img.gz' if name.endswith('.img.gz') else '.' + name.split('.', 1)[1]))


class PublicationTests(unittest.TestCase):
    def test_collect_only_public_assets_and_put_diagnostics_in_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / 'openwrt'
            profile = build.read_toml(build.ROOT / 'Config/profiles.toml')['profiles']['s20p-mt76']
            identity = dict(profile_id='s20p-mt76', repository_owner='owner', build_date='26.10.12-09.00.00',
                            built_at='2026-10-12T01:00:00Z', run_id='123', run_attempt='1', ci_commit='e' * 40)
            lock = dict(profile_id='s20p-mt76', profile=profile, defaults=build.defaults('s20p-mt76'), config={},
                        source=dict(repository='https://github.com/owner/source', branch='main', commit='f' * 40),
                        ci_hash='a' * 64, fingerprint='b' * 64, requested_packages={})
            build.write_json(root / 'lock.json', lock)
            build.write_json(tree / '.build-identity.json', identity)
            fixture(tree, '.config', 'CONFIG_PACKAGE_luci=y\n')
            target = tree / 'bin/targets/mediatek/filogic'
            fixture(target, 'immortalwrt-supergateway_s20p-squashfs-sysupgrade.bin', 'image')
            manifest = fixture(target, 'immortalwrt-mediatek-filogic.manifest', 'luci - 1\n')
            fixture(target, 'profiles.json', '{}')
            fixture(target, 'config.buildinfo', 'redundant')
            fixture(target, 'immortalwrt-supergateway_s20p-sysupgrade.json', '{}')
            with patch.object(build, 'ROOT', root), patch.object(build, 'validate_config'), \
                    patch.object(build, 'validate_image_packages', return_value=manifest), patch.object(build, 'run', return_value='diff'):
                build.collect(SimpleNamespace(lock=root / 'lock.json', source=tree, output_dir=root / 'out'))
            output = root / 'out'
            info = build.read_json(output / 'build-info.json')
            assets = release.release_assets(output, info)
            self.assertEqual(len(assets), 5)
            self.assertNotIn('release-notes.md', [p.name for p in assets])
            self.assertNotIn('profiles.json', [p.name for p in assets])
            self.assertFalse((output / 'packages.tar.gz').exists())
            self.assertTrue((root / 'logs/metadata/profiles.json').is_file())
            self.assertEqual(info['built_at'], identity['built_at'])
            for line in (output / 'sha256sums').read_text().splitlines():
                digest, name = line.split('  ')
                self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())
            self.assertIn('<!-- build-ci:' + lock['ci_hash'], (output / 'release-notes.md').read_text())

    def test_release_allowlist_rejects_paths_missing_files_or_bad_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, 'build-info.json', '{}')
            fixture(root, 'sha256sums', 'a' * 64 + '  build-info.json\n')
            good = ['build-info.json', 'sha256sums']
            self.assertEqual(len(release.release_assets(root, {'release_assets': good})), 2)
            for names in (good + ['../secret'], good + ['missing'], good + ['release-notes.md'], good + ['build-info.json']):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    release.release_assets(root, {'release_assets': names})
            fixture(root, 'firmware.bin', 'image')
            with self.assertRaisesRegex(ValueError, 'exactly'):
                release.release_assets(root, {'release_assets': good + ['firmware.bin']})

    def test_old_artifact_recovery_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root, 'config.full', 'config')
            fixture(root, 'release-notes.md', 'notes')
            self.assertEqual(len(release.release_assets(root, {})), 2)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.values = dict(releases_keep_per_profile=1, release_recovery_days=1,
                           cache_keep_generations=2, cache_min_age_days=1)
        self.profiles = ['s20p-mtk', 'x86-64']

    def make_release(self, profile, number, age=30):
        return dict(id=number, tag_name=f'{profile}-r{number}-1', draft=False, prerelease=False,
                    published_at=(self.now - timedelta(days=age, hours=number)).isoformat(),
                    body='<!-- build-fingerprint:' + 'a' * 64 + ' -->')

    def test_release_retention_is_per_model_and_protects_recent_recovery(self):
        releases = [self.make_release(p, n) for p in self.profiles for n in range(1, 7)]
        candidates = cleanup.release_candidates(releases, self.profiles, self.values, self.now)
        self.assertEqual({r['tag_name'] for r in candidates}, {f'{p}-r{n}-1' for p in self.profiles for n in range(2, 7)})
        recent = [self.make_release('s20p-mtk', n, age=2) for n in range(1, 9)]
        self.assertEqual({r['tag_name'] for r in cleanup.release_candidates(recent, self.profiles, self.values, self.now)},
                         {f's20p-mtk-r{n}-1' for n in range(2, 9)})

    def test_unknown_drafts_prereleases_and_manual_releases_are_untouched(self):
        releases = [self.make_release('s20p-mtk', n, age=0.5) for n in range(1, 7)]
        releases[4]['draft'] = True
        releases[5]['body'] = 'manual'
        releases += [self.make_release('unknown', 7)]
        preview = self.make_release('s20p-mtk', 8, age=0.5)
        preview['prerelease'] = True
        releases.append(preview)
        self.assertEqual(cleanup.release_candidates(releases, self.profiles, self.values, self.now), [])

    def test_cache_retention_separates_model_kind_and_ref(self):
        caches = []
        for profile in self.profiles:
            for ref in ('refs/heads/main', 'refs/heads/test'):
                for kind in ('firmware', 'toolchain'):
                    for n in range(1, 5):
                        prefix = f'firmware-Linux-{profile}-' + 'a' * 64 if kind == 'firmware' else f'toolchain-v1-Linux-ubuntu24-{profile}-' + 'b' * 24 + '-' + 'c' * 40
                        caches.append(dict(id=len(caches) + 1, key=f'{prefix}-{n}-1', ref=ref,
                                           created_at=(self.now - timedelta(days=n + 1)).isoformat(),
                                           last_accessed_at=(self.now - timedelta(days=2)).isoformat()))
        selected = cleanup.cache_candidates(caches, self.profiles, self.values, self.now)
        self.assertEqual(len(selected), 16)
        self.assertTrue(all(c['key'].endswith(('-3-1', '-4-1')) for c in selected))
        caches.append(dict(caches[-1], id=999, key='unrecognized'))
        caches[-2]['last_accessed_at'] = self.now.isoformat()
        selected = cleanup.cache_candidates(caches, self.profiles, self.values, self.now)
        self.assertNotIn(999, [c['id'] for c in selected])
        self.assertNotIn(caches[-2]['id'], [c['id'] for c in selected])

    def test_active_build_skips_all_mutation(self):
        with patch.object(cleanup, 'active_builds', return_value=[{}]), patch.object(cleanup, 'settings', return_value=self.values), \
                patch.object(cleanup, 'read_toml', return_value={'profiles': self.profiles}), \
                patch.dict(cleanup.os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
                patch('sys.argv', ['cleanup.py', '--apply']), patch.object(cleanup, 'api') as api:
            cleanup.main()
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
