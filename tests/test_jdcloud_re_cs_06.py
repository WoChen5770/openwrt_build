import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from Scripts.build import (
    ROOT, defaults, merge_config, read_toml, validate_config, validate_image_packages,
)


class JDCloudRECS06Tests(unittest.TestCase):
    profile_id = "jdcloud-re-cs-06"

    @classmethod
    def setUpClass(cls):
        cls.profiles = read_toml(ROOT / "Config/profiles.toml")["profiles"]
        cls.profile = cls.profiles[cls.profile_id]
        cls.seed = (ROOT / cls.profile["config"]).read_text()
        cls.values = merge_config([cls.seed])

    def test_hardware_selection(self):
        self.assertEqual(self.profile["source"], "viking")
        self.assertEqual(self.profile["target"], "qualcommbe")
        self.assertEqual(self.profile["subtarget"], "ipq53xx")
        self.assertEqual(self.profile["device"], "jdcloud_re-cs-06")
        self.assertEqual(self.values["CCACHE"], "y")
        validate_config(self.profile_id, self.profile, self.seed, self.values)
        for key in self.profile["required"]:
            self.assertEqual(self.values.get(key), "y", key)
        for key in self.profile["forbidden"]:
            with self.subTest(forbidden=key), self.assertRaises(ValueError):
                validate_config(self.profile_id, self.profile, self.seed + f"\nCONFIG_{key}=y\n", self.values)

    def test_s20p_plugin_selections_preserved(self):
        reference = merge_config([(ROOT / self.profiles["s20p-mt76"]["config"]).read_text()])
        hardware = {"PACKAGE_kmod-mt7915e", "PACKAGE_kmod-mt7986-firmware", "PACKAGE_kmod-mmc"}
        for key, value in reference.items():
            if key.startswith("PACKAGE_") and key not in hardware:
                self.assertEqual(self.values.get(key), value, key)
        for key in ("KERNEL_DEBUG_INFO_BTF", "DAED_USE_KERNEL_BTF", "BPF_TOOLCHAIN_HOST"):
            self.assertEqual(self.values[key], "y")

    def test_plugin_sources_match_s20p(self):
        packages = read_toml(ROOT / "Config/packages.toml")

        def selected(profile_id):
            return {name for name, spec in packages.items()
                    if "all" in spec["profiles"] or profile_id in spec["profiles"]}

        self.assertEqual(selected(self.profile_id), selected("s20p-mt76"))

    def test_workflow_defaults_and_script_gates(self):
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        options = next(line for line in workflow.splitlines() if "options: [" in line)
        self.assertIn(self.profile_id, options)
        settings = defaults(self.profile_id)
        self.assertEqual(settings["hostname"], "JDCLOUD-RE-CS-06")
        self.assertTrue(settings["network_tuning"])
        # An empty tree must hit the safety check, not silently skip integration.
        with tempfile.TemporaryDirectory() as temporary:
            env = {**os.environ, "WRT_CONFIG": self.profile["legacy_name"], "GITHUB_WORKSPACE": str(ROOT)}
            for script in ("Mesh.sh", "Honk.sh"):
                with self.subTest(script=script):
                    result = subprocess.run(["bash", str(ROOT / "Scripts" / script)], cwd=temporary,
                                            env=env, text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("missing ./package", result.stderr)

    def test_image_package_validation(self):
        required = self.profile["required_image_packages"]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / ("immortalwrt-qualcommbe-ipq53xx-" + self.profile["image_match"] + ".manifest")
            manifest.write_text("".join(name + " - 1\n" for name in required))
            self.assertEqual(validate_image_packages(Path(temporary), self.profile, self.profile_id), manifest)
            manifest.write_text("".join(name + " - 1\n" for name in required[1:]))
            with self.assertRaisesRegex(ValueError, "missing packages"):
                validate_image_packages(Path(temporary), self.profile, self.profile_id)
            manifest.write_text("".join(name + " - 1\n" for name in required) + "kmod-mt7915e - 1\n")
            with self.assertRaisesRegex(ValueError, "forbidden packages"):
                validate_image_packages(Path(temporary), self.profile, self.profile_id)


if __name__ == "__main__":
    unittest.main()
