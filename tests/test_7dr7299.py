"""TL-7DR7299 v1 source, platform, customization and publication regressions."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Scripts import build
from test_hardware_profiles import serialize
from test_build import fixture


VARIANTS = ("7dr7299-mt76", "7dr7299-mtk")


class SevenDrProfileTests(unittest.TestCase):
    def setUp(self):
        self.specs = build.read_toml(build.ROOT / "Config/profiles.toml")
        self.packages = build.read_toml(build.ROOT / "Config/packages.toml")

    def valid_config(self, name):
        profile = self.specs["profiles"][name]
        values = build.config_for(profile)
        values.update({key: "y" for key in build.required_for(name, profile)})
        return values

    def test_sources_targets_and_workflow_choices(self):
        expected = {
            VARIANTS[0]: ("https://github.com/VIKINGYFY/immortalwrt.git", "owrt", "filogic"),
            VARIANTS[1]: ("https://github.com/padavanonly/immortalwrt-mt798x-6.6.git", "mt798x-mt799x-6.6-mtwifi", "filogic_a73"),
        }
        for name, (repository, branch, subtarget) in expected.items():
            profile = self.specs["profiles"][name]
            self.assertEqual(self.specs["sources"][profile["source"]], dict(repository=repository, branch=branch))
            self.assertEqual((profile["target"], profile["subtarget"], profile["device"]),
                             ("mediatek", subtarget, "tplink_tl-7dr7299-v1"))
            values = build.config_for(profile)
            devices = [key for key, value in values.items() if key.startswith("TARGET_DEVICE_") and value == "y"]
            self.assertEqual(devices, ["TARGET_DEVICE_mediatek_" + subtarget + "_DEVICE_tplink_tl-7dr7299-v1"])
            for workflow in ("build.yml", "release.yml"):
                self.assertIn(name, (build.ROOT / ".github/workflows" / workflow).read_text())
            build.defaults(name)
            build.validate_config(name, profile, serialize(self.valid_config(name)))
        self.assertIn("/bin/targets/**/*.itb", (build.ROOT / ".github/workflows/build-core.yml").read_text())
        self.assertEqual(self.specs["profiles"][VARIANTS[0]]["source"], "viking")

    def test_hardware_seed_config_and_manifest_guards(self):
        for name in VARIANTS:
            profile = self.specs["profiles"][name]
            values = self.valid_config(name)
            seed = build.config_for(profile)
            self.assertEqual(seed["TARGET_ROOTFS_INITRAMFS"], "y")
            for package in profile["required_image_packages"]:
                key = "PACKAGE_" + package
                self.assertEqual(seed[key], "y")
                self.assertIn(key, build.required_for(name, profile))
                for state in (None, "m", "n"):
                    with self.subTest(profile=name, package=package, state=state):
                        actual = dict(values)
                        actual.pop(key)
                        if state:
                            actual[key] = state
                        with self.assertRaisesRegex(ValueError, "Missing CONFIG_" + key + "=y"):
                            build.validate_config(name, profile, serialize(actual))
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                manifest = target / ("immortalwrt-" + profile["image_match"] + ".manifest")
                installed = set(profile["required_image_packages"])
                for missing in installed:
                    manifest.write_text("".join(p + " - 1\n" for p in sorted(installed - {missing})))
                    with self.assertRaisesRegex(ValueError, "Firmware is missing packages"):
                        build.validate_image_packages(target, profile, name)
                for forbidden in profile["forbidden_image_packages"]:
                    manifest.write_text("".join(p + " - 1\n" for p in sorted(installed | {forbidden})))
                    with self.assertRaisesRegex(ValueError, "Firmware contains forbidden packages"):
                        build.validate_image_packages(target, profile, name)

    def test_vendor_platform_is_not_s20p(self):
        profile = self.specs["profiles"][VARIANTS[1]]
        self.assertEqual(profile["config"], "Config/TPLINK-7DR7299-MTK.txt")
        device_values = build.merge_config([(build.ROOT / "Config/TPLINK-7DR7299-MTK.txt").read_text()])
        values = self.valid_config(VARIANTS[1])
        self.assertEqual(values["WARP_VERSION"], '"3"')
        self.assertEqual(values["MTK_WIFI7_MT_WIFI"], "m")
        self.assertEqual(values["MTK_WIFI7_SKU_TYPE"], '"BE7200"')
        for key, expected in profile["expected_config"].items():
            self.assertEqual(values[key], expected)
            if key != "TARGET_ROOTFS_INITRAMFS":
                self.assertEqual(device_values[key], expected)
            with self.subTest(option=key), self.assertRaisesRegex(ValueError, "Expected CONFIG_" + key):
                build.validate_config(VARIANTS[1], profile, serialize({**values, key: "n"}))
        for key in profile["forbidden"]:
            with self.subTest(option=key), self.assertRaisesRegex(ValueError, "Forbidden CONFIG_" + key):
                build.validate_config(VARIANTS[1], profile, serialize({**values, key: "y"}))

    def test_plugin_selection_keeps_upstream_go(self):
        for package in ("passwall", "daed", "easytier", "honk"):
            for name in VARIANTS:
                self.assertIn(name, self.packages[package]["profiles"])
        self.assertIn(VARIANTS[0], self.packages["mesh"]["profiles"])
        self.assertNotIn(VARIANTS[1], self.packages["mesh"]["profiles"])
        self.assertNotIn("golang-7dr7299", self.packages)
        self.assertTrue(all(package["mode"] != "golang-backport" for package in self.packages.values()))
        self.assertIn("TPLINK-7DR7299-MT76", (build.ROOT / "Scripts/Mesh.sh").read_text())
        self.assertNotIn("TPLINK-7DR7299-MTK", (build.ROOT / "Scripts/Mesh.sh").read_text())
        for name in ("TPLINK-7DR7299-MT76", "TPLINK-7DR7299-MTK"):
            self.assertIn(name, (build.ROOT / "Scripts/Honk.sh").read_text())

    def test_plan_all_locks_new_profiles_and_separates_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            args = SimpleNamespace(profile="all", commit=None, event="workflow_dispatch", force=True,
                                   packages="", output_dir=output, github_output=None)
            with patch.object(build, "resolve", side_effect=lambda repo, branch=None, commit=None: commit or hashlib.sha1(repo.encode()).hexdigest()), \
                    patch.object(build, "request", return_value=b"src-git packages https://github.com/immortalwrt/packages.git;master\n"), \
                    patch.object(build, "xray_lock", return_value=dict(tag="v1", version="1", asset="test.zip", asset_id=1, sha256="a" * 64)), \
                    patch.object(build, "ci_hash", return_value="b" * 64):
                build.plan(args)
            matrix = build.read_json(output / "matrix.json")["matrix"]["include"]
            self.assertEqual({row["profile"] for row in matrix}, set(self.specs["profiles"]))
            self.assertEqual(len({row["fingerprint"] for row in matrix}), len(matrix))
            open_lock = build.read_json(output / (VARIANTS[0] + ".json"))
            vendor_lock = build.read_json(output / (VARIANTS[1] + ".json"))
            self.assertNotEqual(open_lock["source"]["commit"], vendor_lock["source"]["commit"])
            self.assertEqual(open_lock["source"], build.read_json(output / "s20p-mt76.json")["source"])
            self.assertNotIn("golang-7dr7299", open_lock["packages"])
            self.assertNotIn("golang-7dr7299", vendor_lock["packages"])


class SevenDrPreparationTests(unittest.TestCase):
    def test_vendor_shell_defaults_keep_hardware_and_restore_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            fixture(tree, "package/base-files/files/bin/config_generate", "hostname='ImmortalWrt'\n192.168.1.1\n")
            wifi = fixture(tree, "package/mtk/applications/mtwifi-cfg/files/mtwifi.sh",
                           'config_get type ${dev} type\n[ "$type" = "mtwifi" ] || {\n'
                           'htmode="EHT160"\nset wireless.${dev}.channel=${channel}\n'
                           '\tset wireless.default_${dev}.ssid=${ssid}\n'
                           '\tset wireless.default_${dev}.encryption=none\n}\n')
            for name in ("daed", "honk"):
                fixture(tree, "package/" + name + "/files/" + name + ".config", "config " + name + "\n\toption enabled '1'\n")
            values = build.defaults(VARIANTS[1])
            build.apply_defaults(tree, values, vendor=True, wireless_defaults="mtwifi-shell")
            result = wifi.read_text()
            for key in ("ssid", "encryption", "key", "disabled"):
                value = {"ssid": values["wifi_ssid"], "encryption": values["wifi_encryption"],
                         "key": values["wifi_password"], "disabled": str(int(values["wifi_disabled"]))}[key]
                self.assertIn("set wireless.default_${dev}." + key + "='" + value + "'", result)
            self.assertIn('[ "$type" = "mtwifi" ] || {', result)
            self.assertIn('htmode="EHT160"', result)
            self.assertIn("set wireless.${dev}.channel=${channel}", result)
            script = (tree / "files/etc/uci-defaults/95-project-defaults").read_text()
            self.assertNotIn("wireless", script)
            for name in ("daed", "honk"):
                self.assertIn("option enabled '0'", (tree / "package" / name / "files" / (name + ".config")).read_text())
            wifi.write_text(result.replace(".ssid=", ".renamed="))
            with self.assertRaisesRegex(ValueError, "Vendor shell Wi-Fi ssid defaults changed"):
                build.apply_defaults(tree, values, vendor=True, wireless_defaults="mtwifi-shell")

    def test_kernel_patch_is_idempotent_scoped_and_fails_on_drift(self):
        profiles = build.read_toml(build.ROOT / "Config/profiles.toml")["profiles"]
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            untouched = fixture(tree, "target/linux/other/config-6.6", "CONFIG_OTHER=y\n")
            for name in VARIANTS:
                profile = profiles[name]
                path = fixture(tree, profile["kernel_config"]["path"], "# CONFIG_BPF_SYSCALL is not set\nCONFIG_BPF_JIT=n\nCONFIG_KEEP=y\n")
                build.apply_kernel_config(tree, profile)
                first = path.read_text()
                self.assertEqual(build.merge_config([first]), {"KEEP": "y", "BPF_JIT": "y", "BPF_SYSCALL": "y", "NET_SCH_BPF": "y"})
                build.apply_kernel_config(tree, profile)
                self.assertEqual(path.read_text(), first)
            self.assertEqual(untouched.read_text(), "CONFIG_OTHER=y\n")
            build.apply_kernel_config(tree, profiles["s20p-mtk"])
            with self.assertRaisesRegex(ValueError, "Kernel config changed upstream"):
                build.apply_kernel_config(tree, {"kernel_config": {"path": "target/linux/missing/config-6.6", "options": {}}})
            with self.assertRaisesRegex(ValueError, "Invalid kernel config path"):
                build.apply_kernel_config(tree, {"kernel_config": {"path": "../outside", "options": {}}})

    def test_integration_preserves_upstream_go_and_consumer_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "package").mkdir()
            compiler = fixture(tree, "feeds/packages/lang/golang/Makefile", "upstream compiler\n")
            helper = fixture(tree, "feeds/packages/lang/golang/golang-package.mk", "upstream helper\n")
            recipe = "PKG_BUILD_DEPENDS:=golang/host\ninclude $(TOPDIR)/feeds/packages/lang/golang/golang-package.mk\n"
            consumer = fixture(tree, "feeds/packages/net/test/Makefile", recipe)
            package = dict(repository="https://example.invalid/daed.git", commit="a" * 40,
                           mode="nested", directories=["daed"])
            lock = dict(profile=dict(legacy_name="TPLINK-7DR7299-MTK"), packages=dict(daed=package))
            with patch.object(build, "checkout", side_effect=lambda repo, commit, clone: fixture(clone, "daed/Makefile", recipe)), \
                    patch.object(build, "run") as run:
                build.integrate(lock, tree)
                run.assert_not_called()
            self.assertEqual(compiler.read_text(), "upstream compiler\n")
            self.assertEqual(helper.read_text(), "upstream helper\n")
            self.assertEqual(consumer.read_text(), recipe)
            self.assertEqual((tree / "package/daed/Makefile").read_text(), recipe)
            self.assertFalse((tree / "feeds/packages/lang/golang1.26").exists())

    def test_prepare_uses_vendor_shell_stack_for_padavan_source(self):
        profile = build.read_toml(build.ROOT / "Config/profiles.toml")["profiles"][VARIANTS[1]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "openwrt"
            tree.mkdir()
            lock = dict(ci_hash="locked", repository_owner="owner", profile_id=VARIANTS[1], profile=profile,
                        source=dict(commit="a" * 40, feeds=[], branch="test"), defaults=build.defaults(VARIANTS[1]),
                        config={}, packages={}, requested_packages={})
            build.write_json(root / "lock.json", lock)
            with patch.object(build, "ROOT", root), patch.object(build, "ci_hash", return_value="locked"), \
                    patch.object(build, "run", return_value="a" * 40), patch.object(build, "integrate"), \
                    patch.object(build, "apply_defaults") as defaults, patch.object(build, "apply_kernel_config") as kernel, \
                    patch.object(build, "customize_luci"), patch.object(build, "validate_config"):
                build.prepare(SimpleNamespace(lock=root / "lock.json", source=tree, toolchain_cache=False))
            defaults.assert_called_once_with(tree.resolve(), lock["defaults"], True, "mtwifi-shell")
            kernel.assert_called_once_with(tree.resolve(), profile)


class SevenDrPublicationTests(unittest.TestCase):
    def test_fit_sysupgrade_and_recovery_are_separate_public_assets(self):
        for name in VARIANTS:
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tree = root / "openwrt"
                profile = build.read_toml(build.ROOT / "Config/profiles.toml")["profiles"][name]
                identity = dict(profile_id=name, repository_owner="owner", build_date="26.10.13-22.00.00", run_id="123", run_attempt="1")
                lock = dict(profile_id=name, profile=profile, defaults=build.defaults(name), config=build.config_for(profile),
                            source=dict(repository="https://github.com/owner/source.git", branch="test", commit="a" * 40),
                            fingerprint="b" * 64, ci_hash="c" * 64)
                build.write_json(root / "lock.json", lock)
                build.write_json(tree / ".build-identity.json", identity)
                fixture(tree, ".config", serialize(lock["config"]))
                target = tree / "bin/targets" / profile["target"] / profile["subtarget"]
                for kind in ("sysupgrade", "recovery"):
                    fixture(target, "immortalwrt-" + profile["image_match"] + "-" + kind + ".itb", "fit image")
                for filename in ("preloader.bin", "bl31-uboot.fip", "unrelated-sysupgrade.itb"):
                    fixture(target, filename, "excluded")
                fixture(target, "immortalwrt-" + profile["image_match"] + ".manifest",
                        "".join(package + " - 1\n" for package in profile["required_image_packages"]))
                with patch.object(build, "ROOT", root), patch.object(build, "run", return_value=""), \
                        patch.object(build, "validate_config"), patch.object(build, "validate_mtk_sysupgrade") as tar_check:
                    build.collect(SimpleNamespace(lock=root / "lock.json", source=tree, output_dir=root / "out"))
                    tar_check.assert_not_called()
                    (target / ("immortalwrt-" + profile["image_match"] + "-sysupgrade.itb")).unlink()
                    with self.assertRaisesRegex(ValueError, "Expected one 7DR7299 sysupgrade.itb"):
                        build.collect(SimpleNamespace(lock=root / "lock.json", source=tree, output_dir=root / "missing"))
                info = build.read_json(root / "out/build-info.json")
                self.assertEqual(len(info["images"]), 2)
                self.assertTrue(all(name in image and image.endswith(".itb") for image in info["images"]))
                for line in (root / "out/sha256sums").read_text().splitlines():
                    checksum, asset = line.split("  ")
                    self.assertEqual(checksum, hashlib.sha256((root / "out" / asset).read_bytes()).hexdigest())
                notes = (root / "out/release-notes.md").read_text()
                self.assertIn("TL-7DR7299 v1", notes)
                self.assertIn("不保证直接互刷", notes)
                self.assertIn("不发布引导器", notes)
                if name == VARIANTS[1]:
                    self.assertIn("daed / honk 默认关闭", notes)


if __name__ == "__main__":
    unittest.main()
