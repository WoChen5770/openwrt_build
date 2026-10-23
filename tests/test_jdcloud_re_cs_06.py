import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from Scripts.build import (
    ROOT, apply_source_patches, defaults, merge_config, read_toml, release_notes,
    validate_config, validate_image_packages, validate_jdcloud_sysupgrade,
)


# Device definition from VIKINGYFY/immortalwrt 4bec1cb (the failed build's source).
JDCLOUD_DEVICE = """define Device/jdcloud_re-cs-06
	$(call Device/FitImage)
	$(call Device/EmmcImage)
	DEVICE_VENDOR := JDCloud
	DEVICE_MODEL := RE-CS-06
	DEVICE_DTS_CONFIG := config@mi01.6
	KERNEL_SIZE := 7168k
	SOC := ipq5322
	SUPPORTED_DEVICES += jdcloud,re-cs-06
	DEVICE_PACKAGES := ipq-wifi-jdcloud_re-cs-06
	IMAGE/factory.bin := append-kernel | pad-to $$(KERNEL_SIZE) | append-rootfs | append-metadata
endef
TARGET_DEVICES += jdcloud_re-cs-06
"""

FIT_LISTING = """FIT description: ARM64 OpenWrt FIT (Flattened Image Tree)
 Image 0 (kernel-1)
  Type:         Kernel Image
  Compression:  lzma compressed
 Image 1 (fdt-1)
  Type:         Flat Device Tree
  Compression:  uncompressed
"""


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

    def test_lzma_patch_only_changes_jdcloud_compression(self):
        before = ("define Device/xiaomi_be6500\n\t$(call Device/FitImage)\nendef\n\n" + JDCLOUD_DEVICE
                  + "\ndefine Device/jdcloud_re-cs-08\n\t$(call Device/FitImage)\nendef\n")
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            image = tree / "target/linux/qualcommbe/image/ipq53xx.mk"
            image.parent.mkdir(parents=True)
            image.write_text(before)
            apply_source_patches(tree, self.profile)
            expected = before.replace("define Device/jdcloud_re-cs-06\n\t$(call Device/FitImage)",
                                      "define Device/jdcloud_re-cs-06\n\t$(call Device/FitImageLzma)")
            self.assertEqual(image.read_text(), expected)
            image.write_text(before.replace("KERNEL_SIZE := 7168k", "KERNEL_SIZE := 8192k"))
            with self.assertRaises(subprocess.CalledProcessError):
                apply_source_patches(tree, self.profile)

    def test_patch_is_scoped_and_paths_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            for name, profile in self.profiles.items():
                if name != self.profile_id:
                    self.assertFalse(profile.get("patches"), name)
                    apply_source_patches(tree, profile)
            for name in ("/tmp/patch", "Scripts/Patches/../../patch", "Config/patch"):
                with self.subTest(path=name), self.assertRaisesRegex(ValueError, "Invalid source patch path"):
                    apply_source_patches(tree, {"patches": [name]})

    def test_sysupgrade_kernel_format_and_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            image = tree / "sysupgrade.bin"

            def write_image(size, names=("sysupgrade-jdcloud_re-cs-06/kernel",)):
                with tarfile.open(image, "w") as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.size = size
                        archive.addfile(member, io.BytesIO(b"\0" * size))

            write_image(7168 * 1024)
            with patch("Scripts.build.run", return_value=FIT_LISTING) as listing:
                validate_jdcloud_sysupgrade(image, tree)
                self.assertEqual(listing.call_args.args[:2], (str(tree / "staging_dir/host/bin/mkimage"), "-l"))
            with patch("Scripts.build.run", return_value=FIT_LISTING.replace("lzma compressed", "gzip compressed")):
                with self.assertRaisesRegex(ValueError, "LZMA-compressed"):
                    validate_jdcloud_sysupgrade(image, tree)
            with patch("Scripts.build.run", return_value="Compression: lzma compressed"):
                with self.assertRaisesRegex(ValueError, "LZMA-compressed"):
                    validate_jdcloud_sysupgrade(image, tree)
            for size in (0, 7168 * 1024 + 1):
                write_image(size)
                with self.subTest(size=size), self.assertRaisesRegex(ValueError, "7 MiB"):
                    validate_jdcloud_sysupgrade(image, tree)
            for names in ((), ("a/kernel", "b/kernel")):
                write_image(16, names)
                with self.subTest(names=names), self.assertRaisesRegex(ValueError, "Expected one"):
                    validate_jdcloud_sysupgrade(image, tree)

    def test_release_notes_warn_about_bootloader(self):
        source = read_toml(ROOT / "Config/profiles.toml")["sources"][self.profile["source"]]
        lock = dict(profile_id=self.profile_id, profile=self.profile, defaults=defaults(self.profile_id),
                    source={**source, "commit": "0" * 40}, config=self.values, fingerprint="test", ci_hash="test")
        notes = release_notes(lock)
        self.assertIn("LZMA/FIT", notes)
        self.assertIn("7 MiB", notes)
        self.assertIn("引导器", notes)


if __name__ == "__main__":
    unittest.main()
