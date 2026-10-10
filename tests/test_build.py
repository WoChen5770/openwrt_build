import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build', ROOT / 'scripts/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.profiles = build.read_json(ROOT / 'profiles.json')['profiles']

    def test_five_distinct_profiles(self):
        self.assertEqual(len(self.profiles), 5)
        self.assertEqual(self.profiles['s20p-mtk']['device'], 'clx_s20p')
        self.assertEqual(self.profiles['s20p-mt76']['device'], 'supergateway_s20p')

    def test_bandix_disabled_everywhere(self):
        for profile in self.profiles.values():
            config = build.config_for(profile)
            self.assertEqual(config['PACKAGE_bandix'], 'n')
            self.assertEqual(config['PACKAGE_luci-app-bandix'], 'n')
        packages = build.read_json(ROOT / 'packages.json')
        self.assertFalse(any('bandix' in json.dumps(p).lower() for p in packages.values()))

    def test_mtk_requested_plugins_enabled(self):
        config = build.config_for(self.profiles['s20p-mtk'])
        for key in ('PACKAGE_luci-app-mesh', 'PACKAGE_daed', 'PACKAGE_luci-app-honk',
                    'PACKAGE_honk', 'KERNEL_DEBUG_INFO_BTF'):
            self.assertEqual(config[key], 'y')

    def test_default_ssid(self):
        values = build.defaults()
        self.assertEqual(values['wifi_ssid'], 'ImmortalWrt')
        self.assertEqual(values['lan_ip'], '192.168.123.1')
        self.assertEqual(values['theme'], 'shadcn')

    def test_model_hostnames_and_config_paths(self):
        expected = {'xiaomi-be6500': 'XIAOMI-BE6500', 'tenda-be12-pro': 'TENDA-BE12-PRO',
                    's20p-mt76': 'SUPERGATEWAY-S20P-MT76', 's20p-mtk': 'SUPERGATEWAY-S20P-MTK',
                    'x86-64': 'X86'}
        for profile_id, hostname in expected.items():
            self.assertEqual(build.defaults(profile_id)['hostname'], hostname)
            self.assertTrue(build.config_for(self.profiles[profile_id]))
        for variant in ('mt76', 'mtk'):
            self.assertIn('Config/SUPERGATEWAY-S20P-' + variant.upper() + '.txt',
                          self.profiles['s20p-' + variant]['fragments'])

    def test_x86_honk_integration(self):
        config = build.config_for(self.profiles['x86-64'])
        for key in ('PACKAGE_honk', 'PACKAGE_luci-app-honk', 'PACKAGE_luci-i18n-honk-zh-cn',
                    'KERNEL_DEBUG_INFO_BTF', 'KERNEL_XDP_SOCKETS'):
            self.assertEqual(config[key], 'y')
        packages = build.read_json(ROOT / 'packages.json')
        self.assertIn('x86-64', packages['honk']['profiles'])
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, 'run') as run:
            tree = Path(temporary)
            (tree / 'package').mkdir()
            build.integrate({'profile_id': 'x86-64', 'profile': self.profiles['x86-64'],
                             'packages': {'honk': packages['honk']}}, tree)
            args, kwargs = run.call_args
            self.assertTrue(args[1].endswith('/legacy/Honk.sh'))
            self.assertEqual(kwargs['env']['WRT_CONFIG'], 'X86')
        self.assertIn('|X86)', (ROOT / 'legacy/Honk.sh').read_text())

    def test_passwall_xray_only(self):
        for profile_id in ('s20p-mt76', 's20p-mtk', 'x86-64'):
            profile = self.profiles[profile_id]
            config = build.config_for(profile)
            self.assertEqual(config['PACKAGE_luci-app-passwall_INCLUDE_Xray'], 'y')
            self.assertEqual(config['PACKAGE_xray-core'], 'y')
            for key, value in config.items():
                if key.startswith('PACKAGE_luci-app-passwall_INCLUDE_') and not key.endswith('_Xray'):
                    self.assertEqual(value, 'n', key)
            config.update({key: 'y' for key in build.required_for(profile_id, profile)})
            content = '\n'.join('CONFIG_' + key + '=' + value for key, value in config.items())
            build.validate_config(profile_id, profile, content)
            with self.assertRaisesRegex(ValueError, 'Missing CONFIG_PACKAGE_luci-app-passwall_INCLUDE_Xray'):
                build.validate_config(profile_id, profile, content + '\nCONFIG_PACKAGE_luci-app-passwall_INCLUDE_Xray=n')
            for forbidden in ('PACKAGE_hysteria', 'PACKAGE_simple-obfs-client',
                              'PACKAGE_luci-app-passwall_INCLUDE_NaiveProxy',
                              'PACKAGE_luci-app-passwall_INCLUDE_FutureBackend'):
                with self.assertRaisesRegex(ValueError, 'Forbidden CONFIG_' + forbidden):
                    build.validate_config(profile_id, profile, content + '\nCONFIG_' + forbidden + '=m')

    def test_merge_last_value_wins(self):
        self.assertEqual(build.merge_config(['CONFIG_FOO=y\n', '# CONFIG_FOO is not set\nCONFIG_BAR="hi"']),
                         {'FOO': 'n', 'BAR': '"hi"'})

    def test_parse_feeds(self):
        feeds = build.parse_feeds('src-git luci https://github.com/test/luci.git;stable\n'
                                 'src-git-full packages https://github.com/test/packages.git^' + 'a' * 40)
        self.assertEqual(feeds[0]['branch'], 'stable')
        self.assertEqual(feeds[1]['commit'], 'a' * 40)
        for bad in ('src-link luci /tmp/luci', 'src-git luci http://example.org/repo',
                    'src-git luci https://example.org/a\nsrc-git luci https://example.org/b'):
            with self.assertRaises(ValueError):
                build.parse_feeds(bad)

    def test_requirements_and_forbidden_packages(self):
        profile = self.profiles['s20p-mtk']
        config = build.config_for(profile)
        config.update({key: 'y' for key in build.required_for('s20p-mtk', profile)})
        content = '\n'.join('CONFIG_' + key + '=' + value for key, value in config.items())
        build.validate_config('s20p-mtk', profile, content)
        with self.assertRaisesRegex(ValueError, 'bandix'):
            build.validate_config('s20p-mtk', profile, content + '\nCONFIG_PACKAGE_bandix=y')
        with self.assertRaisesRegex(ValueError, 'More than one device'):
            build.validate_config('s20p-mtk', profile, content + '\nCONFIG_TARGET_DEVICE_fake=y')
        with self.assertRaisesRegex(ValueError, 'Missing CONFIG_PACKAGE_honk'):
            build.validate_config('s20p-mtk', profile, content + '\nCONFIG_PACKAGE_honk=n')

    def test_remove_only_exact_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / 'package/custom/bandix').mkdir(parents=True)
            (tree / 'package/custom/not-bandix').mkdir()
            (tree / 'feeds/packages/net/bandix').mkdir(parents=True)
            build.remove_packages(tree, ['bandix'])
            self.assertFalse((tree / 'package/custom/bandix').exists())
            self.assertTrue((tree / 'package/custom/not-bandix').exists())

    def test_invalid_sha(self):
        with self.assertRaises(ValueError):
            build.resolve('unused', commit='main;rm')

    def test_schedule_disabled_without_network(self):
        read_json = build.read_json
        def disabled(path):
            value = read_json(path)
            return {**value, 'scheduled_builds': False} if Path(path).name == 'build-settings.json' else value
        with tempfile.TemporaryDirectory() as temporary, patch.object(build, 'request') as request, \
                patch.object(build, 'read_json', side_effect=disabled):
            build.plan(argparse.Namespace(profile='all', commit=None, event='schedule', force=False,
                                          output_dir=temporary, github_output=None))
            self.assertEqual(build.read_json(Path(temporary) / 'matrix.json')['has_builds'], False)
            request.assert_not_called()

    def scheduled_plan(self, source_changes=(), omit_profiles=()):
        specs = build.read_json(ROOT / 'profiles.json')
        releases = []
        for profile_id, profile in self.profiles.items():
            if profile_id in omit_profiles:
                continue
            source_id = profile['source']
            source = {**specs['sources'][source_id], 'commit': ('b' if source_id in source_changes else 'a') * 40}
            releases.append({'tag_name': profile_id + '-previous', 'draft': False,
                             'body': build.source_marker(source)})
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(build.os.environ, {'GITHUB_REPOSITORY': 'test/build'}), \
                patch.object(build, 'run', return_value=json.dumps([releases])), \
                patch.object(build, 'resolve', return_value='a' * 40), \
                patch.object(build, 'request', return_value=b'') as request, \
                patch.object(build, 'xray_lock', return_value={'tag': 'test'}), \
                patch.object(build, 'ci_hash', return_value='changed-local-config'):
            build.plan(argparse.Namespace(profile='all', commit=None, event='schedule', force=False,
                                          output_dir=temporary, github_output=None))
            result = build.read_json(Path(temporary) / 'matrix.json')
            profiles = [item['profile'] for item in result['matrix']['include']]
            locks = {p: build.read_json(Path(temporary) / (p + '.json')) for p in profiles}
            return profiles, request.call_count, locks

    def test_schedule_enabled_and_unchanged_sources_skipped(self):
        self.assertTrue(build.read_json(ROOT / 'build-settings.json')['scheduled_builds'])
        profiles, requests, _ = self.scheduled_plan()
        self.assertEqual(profiles, [])
        self.assertEqual(requests, 0)

    def test_source_marker_ignores_feeds_and_tracks_source_identity(self):
        source = {'repository': 'https://github.com/test/source.git', 'branch': 'owrt', 'commit': 'a' * 40}
        self.assertEqual(build.source_marker(source), build.source_marker({**source, 'feeds': ['changed']}))
        for field, value in (('repository', 'https://github.com/test/other.git'),
                             ('branch', '25.12'), ('commit', 'b' * 40)):
            self.assertNotEqual(build.source_marker(source), build.source_marker({**source, field: value}))

    def test_schedule_viking_update_only(self):
        profiles, _, locks = self.scheduled_plan(source_changes=('viking',))
        self.assertEqual(set(profiles), {'xiaomi-be6500', 'tenda-be12-pro', 's20p-mt76', 'x86-64'})
        for profile_id, lock in locks.items():
            self.assertEqual(lock['defaults']['hostname'], self.profiles[profile_id]['hostname'])

    def test_schedule_mtk_update_only(self):
        profiles, _, _ = self.scheduled_plan(source_changes=('chasey',))
        self.assertEqual(profiles, ['s20p-mtk'])

    def test_schedule_first_build_and_failed_profile_retry(self):
        profiles, _, _ = self.scheduled_plan(omit_profiles=tuple(self.profiles))
        self.assertEqual(set(profiles), set(self.profiles))
        profiles, _, _ = self.scheduled_plan(omit_profiles=('x86-64',))
        self.assertEqual(profiles, ['x86-64'])

    def test_defaults_patch_both_wifi_stacks(self):
        for vendor in (True, False):
            with self.subTest(vendor=vendor), tempfile.TemporaryDirectory() as temporary:
                tree = Path(temporary)
                generator = tree / 'package/base-files/files/bin/config_generate'
                generator.parent.mkdir(parents=True)
                generator.write_text("ipaddr='192.168.1.1'\nhostname='OpenWrt'\n")
                if vendor:
                    path = tree / 'package/mtk/applications/mtwifi-cfg-ucode/files/lib/wifi/mtwifi.uc'
                    fixture = '{ "ssid": defs.ssid, "encryption": "none" }'
                else:
                    path = tree / 'package/network/config/wifi-scripts/files/lib/wifi/mac80211.uc'
                    fixture = '\n'.join("set ${si}." + key + "='old'" for key in ('ssid', 'key', 'encryption', 'disabled'))
                path.parent.mkdir(parents=True)
                path.write_text(fixture)
                values = build.defaults('s20p-mtk' if vendor else 'x86-64')
                build.apply_defaults(tree, values, vendor)
                self.assertIn('ImmortalWrt', path.read_text())
                self.assertIn('12345678', path.read_text())
                self.assertIn('192.168.123.1', generator.read_text())
                self.assertIn(values['hostname'], generator.read_text())
                script = (tree / 'files/etc/uci-defaults/95-project-defaults').read_text()
                self.assertIn(values['hostname'], script)
                self.assertIn('/luci-static/shadcn', script)
                if vendor:
                    script = (tree / 'files/etc/uci-defaults/98-mtk-experimental-off').read_text()
                    self.assertIn('mesh.main.enabled', script)
                    self.assertIn('mesh.main.button', script)
                    self.assertIn('daed.config.enabled', script)

    def test_upstream_wifi_drift_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            generator = tree / 'package/base-files/files/bin/config_generate'
            generator.parent.mkdir(parents=True)
            generator.write_text("ipaddr='192.168.1.1'\nhostname='OpenWrt'")
            path = tree / 'package/network/config/wifi-scripts/files/lib/wifi/mac80211.uc'
            path.parent.mkdir(parents=True)
            path.write_text('// incompatible upstream')
            with self.assertRaisesRegex(ValueError, 'defaults changed upstream'):
                build.apply_defaults(tree, build.defaults('x86-64'))


if __name__ == '__main__':
    unittest.main()
