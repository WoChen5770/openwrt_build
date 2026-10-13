"""Keep Mesh out of vendor Wi-Fi images without losing temperature support."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from Scripts import build


class MtkMeshTests(unittest.TestCase):
    def setUp(self):
        self.profiles = build.read_toml(build.ROOT / "Config/profiles.toml")["profiles"]
        self.profile = self.profiles["s20p-mtk"]

    def test_mesh_is_excluded_only_from_mtk(self):
        mesh = build.read_toml(build.ROOT / "Config/packages.toml")["mesh"]
        self.assertNotIn("s20p-mtk", mesh["profiles"])
        self.assertNotIn("all", mesh["profiles"])
        self.assertEqual(build.config_for(self.profile)["PACKAGE_luci-app-mesh"], "n")
        for name in ("s20p-mt76", "tenda-be12-pro", "xiaomi-be6500"):
            with self.subTest(profile=name):
                self.assertIn(name, mesh["profiles"])
                self.assertEqual(build.config_for(self.profiles[name])["PACKAGE_luci-app-mesh"], "y")

    def test_temperature_packages_remain_required(self):
        self.assertEqual(build.config_for(self.profile)["PACKAGE_wireless-tools"], "y")
        self.assertIn("PACKAGE_wireless-tools", build.required_for("s20p-mtk", self.profile))
        self.assertIn("wireless-tools", self.profile["required_image_packages"])

    def test_defconfig_cannot_reenable_mesh(self):
        values = build.config_for(self.profile)
        values.update({key: "y" for key in build.required_for("s20p-mtk", self.profile)})
        values["PACKAGE_luci-app-mesh"] = "y"
        content = "\n".join("CONFIG_" + key + "=" + value for key, value in values.items())
        with self.assertRaisesRegex(ValueError, "Forbidden CONFIG_PACKAGE_luci-app-mesh"):
            build.validate_config("s20p-mtk", self.profile, content)

    def test_mesh_integration_skips_mtk_before_fetching(self):
        env = {**os.environ, "WRT_CONFIG": self.profile["legacy_name"]}
        env.pop("GITHUB_WORKSPACE", None)
        env.pop("MESH_COMMIT", None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(["bash", str(build.ROOT / "Scripts/Mesh.sh")],
                                    cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_manifest_rejects_mesh_and_requires_temperature_package(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            manifest = target / "immortalwrt-mediatek-filogic-clx_s20p.manifest"
            packages = set(self.profile["required_image_packages"])

            def write_manifest(selected):
                manifest.write_text("".join(name + " - 1.0\n" for name in sorted(selected)))

            write_manifest(packages)
            self.assertEqual(build.validate_image_packages(target, self.profile, "s20p-mtk"), manifest)
            write_manifest(packages | {"luci-app-mesh"})
            with self.assertRaisesRegex(ValueError, "forbidden packages: luci-app-mesh"):
                build.validate_image_packages(target, self.profile, "s20p-mtk")
            write_manifest(packages - {"wireless-tools"})
            with self.assertRaisesRegex(ValueError, "missing packages: wireless-tools"):
                build.validate_image_packages(target, self.profile, "s20p-mtk")

    def test_vendor_defaults_work_without_mesh_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            fixtures = {
                "package/base-files/files/bin/config_generate": "ipaddr='192.168.1.1'\nhostname='OpenWrt'\n",
                "package/mtk/applications/mtwifi-cfg-ucode/files/lib/wifi/mtwifi.uc":
                    '"ssid": defs.ssid,\n"encryption": "none"\n',
                "package/daed/files/daed.config": "config daed 'config'\n\toption enabled '1'\n",
                "package/honk/files/honk.config": "config honk 'config'\n\toption enabled '1'\n",
            }
            for relative, content in fixtures.items():
                path = tree / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            build.apply_defaults(tree, build.defaults("s20p-mtk"), vendor=True)
            for service in ("daed", "honk"):
                path = tree / "package" / service / "files" / (service + ".config")
                self.assertIn("option enabled '0'", path.read_text())
            self.assertFalse((tree / "package/luci-app-mesh").exists())
            for path in (tree / "files/etc/uci-defaults").iterdir():
                self.assertNotIn("uci -q set mesh.", path.read_text())
                self.assertNotIn("/etc/init.d/mesh", path.read_text())


if __name__ == "__main__":
    unittest.main()
