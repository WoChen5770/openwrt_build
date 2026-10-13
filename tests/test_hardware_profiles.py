"""Keep device-required drivers explicit and unrelated USB packages excluded."""
from pathlib import Path
import tempfile
import unittest

from Scripts import build


# Device packages from VIKINGYFY/immortalwrt at
# 336f7696b76866ca1793a4c4c1478ce40a112b4a, plus BE6500's existing ath12k/PPE stack.
HARDWARE_PACKAGES = {
    "tenda-be12-pro": {
        "kmod-mt7992-firmware", "mt7987-2p5g-phy-firmware",
        "airoha-en8811h-firmware", "kmod-phy-airoha-en8811h",
    },
    "s20p-mt76": {"kmod-mt7915e", "kmod-mt7986-firmware", "kmod-mmc"},
    "xiaomi-be6500": {
        "kmod-ath12k", "kmod-qcom-ppe", "ipq-wifi-xiaomi_be6500",
        "ath12k-firmware-ipq5332-ddwrt", "ath12k-firmware-qcn9274-ddwrt",
    },
}
USB_TRIMMED = ("tenda-be12-pro", "xiaomi-be6500")


def serialize(values):
    # Match defconfig's standard representation of disabled options.
    return "".join(
        "# CONFIG_" + key + " is not set\n" if value == "n"
        else "CONFIG_" + key + "=" + value + "\n"
        for key, value in values.items()
    )


class HardwareProfileTests(unittest.TestCase):
    def setUp(self):
        self.profiles = build.read_json(build.ROOT / "Config/profiles.json")["profiles"]

    def valid_config(self, name):
        profile = self.profiles[name]
        values = build.config_for(profile)
        # Other required feature dependencies can be supplied by upstream.
        values.update({key: "y" for key in build.required_for(name, profile)})
        return values

    def test_hardware_is_explicit_and_guarded_in_config_and_image(self):
        for name, packages in HARDWARE_PACKAGES.items():
            profile = self.profiles[name]
            seed = build.config_for(profile)
            for package in packages:
                with self.subTest(profile=name, package=package):
                    key = "PACKAGE_" + package
                    self.assertEqual(seed.get(key), "y")
                    self.assertIn(key, build.required_for(name, profile))
                    self.assertIn(package, profile["required_image_packages"])

    def test_mtk_vendor_options_are_defined_in_device_config(self):
        profile = self.profiles["s20p-mtk"]
        device = build.ROOT / "Config/SUPERGATEWAY-S20P-MTK.txt"
        values = build.merge_config([device.read_text()])
        for key, expected in profile["expected_config"].items():
            with self.subTest(option=key):
                self.assertEqual(values.get(key), expected)

    def test_hardware_cannot_be_missing_or_only_built_as_module(self):
        for name, packages in HARDWARE_PACKAGES.items():
            profile = self.profiles[name]
            good = self.valid_config(name)
            build.validate_config(name, profile, serialize(good))
            for package in packages:
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

    def test_usb_trimmed_profiles_cannot_force_default_modules_back_on(self):
        for name in USB_TRIMMED:
            profile = self.profiles[name]
            seed = build.config_for(profile)
            with self.subTest(profile=name):
                self.assertEqual(seed["TARGET_PER_DEVICE_ROOTFS"], "n")
                usb_keys = {key for key in seed if key.startswith("PACKAGE_kmod-usb")}
                self.assertTrue(usb_keys)
                for key in usb_keys | {"PACKAGE_automount"}:
                    self.assertEqual(seed[key], "n", key)
                    for state in ("y", "m"):
                        actual = {**self.valid_config(name), key: state}
                        with self.assertRaises(ValueError) as error:
                            build.validate_config(name, profile, serialize(actual))
                        self.assertIn("Forbidden CONFIG_" + key, str(error.exception))
                actual = {**self.valid_config(name), "TARGET_PER_DEVICE_ROOTFS": "y"}
                with self.assertRaisesRegex(ValueError, "Expected CONFIG_TARGET_PER_DEVICE_ROOTFS=n"):
                    build.validate_config(name, profile, serialize(actual))

    def test_s20p_storage_and_usb_policy_is_unchanged(self):
        for name in ("s20p-mt76", "s20p-mtk"):
            with self.subTest(profile=name):
                values = build.config_for(self.profiles[name])
                self.assertEqual(values["TARGET_PER_DEVICE_ROOTFS"], "y")
                for key in ("PACKAGE_kmod-mmc", "PACKAGE_kmod-usb3", "PACKAGE_automount"):
                    self.assertEqual(values[key], "y")

    def test_manifest_requires_each_hardware_package(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name, packages in HARDWARE_PACKAGES.items():
                profile = self.profiles[name]
                manifest = target / ("immortalwrt-" + profile["image_match"] + ".manifest")

                def write_manifest(selected):
                    manifest.write_text("".join(package + " - 1.0\n" for package in sorted(selected)))

                write_manifest(packages)
                self.assertEqual(build.validate_image_packages(target, profile, name), manifest)
                for package in packages:
                    with self.subTest(profile=name, missing=package):
                        write_manifest(packages - {package})
                        with self.assertRaises(ValueError) as error:
                            build.validate_image_packages(target, profile, name)
                        self.assertIn("Firmware is missing packages: " + package, str(error.exception))
                if name in USB_TRIMMED:
                    for package in ("automount", "kmod-usb-core", "kmod-usb3"):
                        with self.subTest(profile=name, forbidden=package):
                            self.assertIn(package, profile["forbidden_image_packages"])
                            write_manifest(packages | {package})
                            with self.assertRaises(ValueError) as error:
                                build.validate_image_packages(target, profile, name)
                            self.assertIn("Firmware contains forbidden packages: " + package, str(error.exception))


if __name__ == "__main__":
    unittest.main()
