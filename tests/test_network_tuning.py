"""Keep early-boot BBR/fq defaults scoped to open-source wireless profiles."""
from pathlib import Path
import tempfile
import unittest

from Scripts import build


OPEN_WIFI = {"xiaomi-be6500", "tenda-be12-pro", "s20p-mt76", "7dr7299-mt76"}
PACKAGES = {"kmod-tcp-bbr", "kmod-sched"}
SETTINGS = {
    "net.ipv4.tcp_congestion_control": "bbr",
    "net.core.default_qdisc": "fq",
    "net.ipv4.tcp_mtu_probing": "1",
}
SYSCTL_PATH = "files/etc/sysctl.d/11-project-network.conf"


def fixture(tree, relative, content):
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def serialize(values):
    return "".join("CONFIG_" + key + "=" + value + "\n" for key, value in values.items())


class NetworkTuningTests(unittest.TestCase):
    def setUp(self):
        self.profiles = build.read_toml(build.ROOT / "Config/profiles.toml")["profiles"]

    def test_only_open_wireless_profiles_opt_in(self):
        self.assertEqual({name for name, profile in self.profiles.items()
                          if profile.get("network_tuning")}, OPEN_WIFI)
        for name in self.profiles:
            with self.subTest(profile=name):
                self.assertEqual(bool(build.defaults(name).get("network_tuning")), name in OPEN_WIFI)

    def test_modules_are_explicit_required_and_installed(self):
        for name in OPEN_WIFI:
            profile = self.profiles[name]
            values = build.config_for(profile)
            for package in PACKAGES:
                with self.subTest(profile=name, package=package):
                    key = "PACKAGE_" + package
                    self.assertEqual(values.get(key), "y")
                    self.assertIn(key, build.required_for(name, profile))
                    self.assertIn(package, profile["required_image_packages"])
            self.assertNotEqual(values.get("PACKAGE_tc-full"), "y")

    def test_defconfig_cannot_drop_or_modularize_required_packages(self):
        for name in OPEN_WIFI:
            profile = self.profiles[name]
            good = build.config_for(profile)
            good.update({key: "y" for key in build.required_for(name, profile)})
            build.validate_config(name, profile, serialize(good))
            for package in PACKAGES:
                key = "PACKAGE_" + package
                for state in (None, "n", "m"):
                    with self.subTest(profile=name, package=package, state=state):
                        actual = dict(good)
                        actual.pop(key)
                        if state is not None:
                            actual[key] = state
                        with self.assertRaises(ValueError) as error:
                            build.validate_config(name, profile, serialize(actual))
                        self.assertIn("Missing CONFIG_" + key + "=y", str(error.exception))

    def test_manifest_cannot_omit_bbr_or_fq_modules(self):
        for name in OPEN_WIFI:
            profile = self.profiles[name]
            packages = set(profile["required_image_packages"])
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)
                manifest = target / ("immortalwrt-" + profile["image_match"] + ".manifest")
                manifest.write_text("".join(package + " - 1\n" for package in sorted(packages)))
                self.assertEqual(build.validate_image_packages(target, profile, name), manifest)
                for missing in PACKAGES:
                    manifest.write_text("".join(package + " - 1\n" for package in sorted(packages - {missing})))
                    with self.subTest(missing=missing), self.assertRaises(ValueError) as error:
                        build.validate_image_packages(target, profile, name)
                    self.assertIn("Firmware is missing packages: " + missing, str(error.exception))

    def prepare_defaults(self, tree):
        fixture(tree, "package/base-files/files/bin/config_generate", "hostname='OpenWrt'\n192.168.1.1\n")
        fixture(tree, "package/network/config/wifi-scripts/files/lib/wifi/mac80211.uc",
                "set ${si}.ssid='OpenWrt'\nset ${si}.key=''\n"
                "set ${si}.encryption='none'\nset ${si}.disabled='1'\n")
        fixture(tree, "package/mtk/applications/mtwifi-cfg-ucode/files/lib/wifi/mtwifi.uc",
                '"ssid": defs.ssid,\n"encryption": "none"\n')
        fixture(tree, "package/mtk/applications/mtwifi-cfg/files/mtwifi.sh",
                "set wireless.default_${dev}.ssid=${ssid}\nset wireless.default_${dev}.encryption=none\n")
        for service in ("daed", "honk"):
            fixture(tree, "package/" + service + "/files/" + service + ".config", "option enabled '1'\n")

    def test_sysctl_overlay_is_persistent_scoped_and_preserves_custom_settings(self):
        for name, profile in self.profiles.items():
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as directory:
                tree = Path(directory)
                self.prepare_defaults(tree)
                untouched = {
                    "files/etc/sysctl.d/99-user.conf": "net.core.default_qdisc=cake\n",
                    "files/etc/sysctl.conf": "net.ipv4.tcp_congestion_control=cubic\n",
                    "files/etc/config/firewall": "option flow_offloading_hw '1'\n",
                    "files/etc/config/network": "option mtu '1492'\n",
                    "files/etc/config/sqm": "option enabled '1'\noption qdisc 'cake'\n",
                }
                for relative, content in untouched.items():
                    fixture(tree, relative, content)
                vendor = profile["variant"] == "mtk-vendor"
                build.apply_defaults(tree, build.defaults(name), vendor, profile.get("wireless_defaults"))
                path = tree / SYSCTL_PATH
                self.assertEqual(path.exists(), name in OPEN_WIFI)
                if name in OPEN_WIFI:
                    expected = (build.ROOT / "Scripts/Files/network-tuning.conf").read_bytes()
                    self.assertEqual(path.read_bytes(), expected)
                    settings = dict(line.split("=", 1) for line in path.read_text().splitlines()
                                    if line and not line.startswith("#"))
                    self.assertEqual(settings, SETTINGS)
                    build.apply_defaults(tree, build.defaults(name))
                    self.assertEqual(path.read_bytes(), expected)
                for relative, content in untouched.items():
                    self.assertEqual((tree / relative).read_text(), content)
                # No new first-boot tuning script: use the existing boot sysctl service.
                scripts = {p.name for p in (tree / "files/etc/uci-defaults").iterdir()}
                self.assertEqual(scripts, {p.name for p in (build.ROOT / "Scripts/uci-defaults").iterdir()}
                                 | {"95-project-defaults"})


if __name__ == "__main__":
    unittest.main()
