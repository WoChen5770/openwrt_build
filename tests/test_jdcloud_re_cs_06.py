import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from Scripts.build import (
    ROOT, apply_source_patches, compression_profile, defaults, merge_config, plan, read_json, read_toml, release_notes,
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
        plugins = {"adguardhome", "daed", "honk", "easytier", "xray-core", "v2ray-geoip", "v2ray-geosite",
                   "dawn", "umdns", "batctl-default", "wpad-openssl", "kmod-batman-adv", "MAC80211_MESH"}
        for key, value in reference.items():
            if key.startswith("PACKAGE_luci") or key.removeprefix("PACKAGE_") in plugins or "_INCLUDE_" in key:
                self.assertEqual(self.values.get(key), value, key)
        for key in ("KERNEL_DEBUG_INFO_BTF", "DAED_USE_KERNEL_BTF", "BPF_TOOLCHAIN_HOST"):
            self.assertEqual(self.values[key], "y")

    def test_unneeded_explicit_packages_pruned(self):
        removed = {"kmod-ata-ahci", "kmod-ata-core", "kmod-nvme", "nvme-cli", "libnvme", "kmod-usb-audio",
                   "kmod-sound-core", "kmod-usb-ehci", "kmod-usb-ohci", "kmod-usb-uhci", "kmod-fs-ksmbd",
                   "kmod-ovpn-backports", "kmod-bonding", "proto-bonding", "kmod-cryptodev", "kmod-tls",
                   "kmod-mtd-rw", "nand-utils", "kmod-wireguard", "usbmuxd", "libimobiledevice",
                   "usb-modeswitch", "kmod-fs-btrfs", "kmod-fuse", "kmod-dummy", "kmod-inet-mptcp-diag",
                   "cfdisk", "cgdisk", "gdisk", "sfdisk", "sgdisk", "smartmontools", "smartmontools-drivedb", "coremark"}
        reference = merge_config([(ROOT / self.profiles["s20p-mt76"]["config"]).read_text()])
        removed |= {key.removeprefix("PACKAGE_") for key in reference if key.startswith("PACKAGE_kmod-usb-net")}
        self.assertEqual(len(removed), 48)
        for name in removed:
            self.assertNotIn("PACKAGE_" + name, self.values, name)

    def test_proxy_and_storage_dependencies_preserved(self):
        for name in ("kmod-nft-queue", "kmod-nft-socket", "kmod-nft-tproxy", "kmod-nft-nat", "kmod-tun",
                     "kmod-veth", "kmod-sched", "kmod-tcp-bbr", "ip-full", "automount", "kmod-usb3",
                     "kmod-usb-dwc3-qcom", "kmod-usb-storage", "kmod-usb-storage-extras", "kmod-usb-storage-uas",
                     "fdisk", "lsblk", "mmc-utils", "exfat-fsck", "exfat-mkfs"):
            self.assertEqual(self.values["PACKAGE_" + name], "y", name)
        for key in ("KERNEL_BPF_EVENTS", "KERNEL_DEBUG_INFO", "KERNEL_DEBUG_INFO_BTF", "KERNEL_XDP_SOCKETS"):
            self.assertEqual(self.values[key], "y", key)

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
            apply_source_patches(tree, compression_profile(self.profile_id, self.profile, "gzip"))
            self.assertEqual(image.read_text(), before)
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
                validate_jdcloud_sysupgrade(image, tree, "gzip")
                with self.assertRaisesRegex(ValueError, "LZMA-compressed"):
                    validate_jdcloud_sysupgrade(image, tree)
            with patch("Scripts.build.run", return_value=FIT_LISTING):
                with self.assertRaisesRegex(ValueError, "GZIP-compressed"):
                    validate_jdcloud_sysupgrade(image, tree, "gzip")
            with patch("Scripts.build.run", return_value="Compression: lzma compressed"):
                with self.assertRaisesRegex(ValueError, "LZMA-compressed"):
                    validate_jdcloud_sysupgrade(image, tree)
            for size in (0, 7168 * 1024 + 1):
                write_image(size)
                for compression in ("lzma", "gzip"):
                    with self.subTest(size=size, compression=compression), self.assertRaisesRegex(ValueError, "7 MiB"):
                        validate_jdcloud_sysupgrade(image, tree, compression)
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
        lock["profile"] = compression_profile(self.profile_id, self.profile, "gzip")
        notes = release_notes(lock)
        self.assertIn("GZIP/FIT", notes)
        self.assertIn("7 MiB", notes)
        self.assertNotIn("LZMA", notes)

    def test_compression_overrides_are_scoped_and_nonmutating(self):
        original = {**self.profile, "patches": list(self.profile["patches"])}
        self.assertEqual(compression_profile(self.profile_id, self.profile), self.profile)
        gzip = compression_profile(self.profile_id, self.profile, "gzip")
        self.assertEqual(gzip["kernel_compression"], "gzip")
        self.assertEqual(gzip["patches"], [])
        restored = compression_profile(self.profile_id, gzip, "lzma")
        self.assertEqual(restored, self.profile)
        self.assertEqual(original, self.profile)
        self.assertEqual(compression_profile(self.profile_id, self.profile, "lzma")["patches"], self.profile["patches"])
        extra = {**self.profile, "patches": [*self.profile["patches"], "Scripts/Patches/other.patch"]}
        self.assertEqual(compression_profile(self.profile_id, extra, "gzip")["patches"], ["Scripts/Patches/other.patch"])
        for name in ("all", "s20p-mt76", "x86-64"):
            with self.subTest(profile=name), self.assertRaisesRegex(ValueError, "requires jdcloud"):
                compression_profile(name, self.profile, "gzip")
        with self.assertRaisesRegex(ValueError, "Invalid kernel compression"):
            compression_profile(self.profile_id, self.profile, "zstd")

    def test_plan_locks_compression_and_separates_concurrency(self):
        locks, matrices = {}, {}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"GITHUB_REPOSITORY": "test/firmware"}), \
                patch("Scripts.build.resolve", return_value="1" * 40), \
                patch("Scripts.build.request", return_value=b""), \
                patch("Scripts.build.ci_hash", return_value="test"), \
                patch("Scripts.build.xray_lock", return_value={"commit": "2" * 40}):
            for compression in ("profile", "lzma", "gzip"):
                output = Path(temporary) / compression
                args = SimpleNamespace(profile=self.profile_id, commit="1" * 40, packages="", event="workflow_dispatch",
                                       force=True, output_dir=str(output), github_output=None, kernel_compression=compression)
                plan(args)
                locks[compression] = read_json(output / (self.profile_id + ".json"))
                matrices[compression] = read_json(output / "matrix.json")["matrix"]["include"][0]
            self.assertEqual(locks["profile"], locks["lzma"])
            self.assertNotEqual(locks["lzma"]["fingerprint"], locks["gzip"]["fingerprint"])
            self.assertNotEqual(matrices["lzma"]["concurrency_group"], matrices["gzip"]["concurrency_group"])
            for compression in ("lzma", "gzip"):
                self.assertEqual(locks[compression]["profile"]["kernel_compression"], compression)
                self.assertEqual(matrices[compression]["kernel_compression"], compression)
            for key in ("source", "packages", "xray", "config", "defaults"):
                self.assertEqual(locks["lzma"][key], locks["gzip"][key], key)
            args.profile = "xiaomi-be6500"
            args.kernel_compression = "profile"
            plan(args)
            matrix = read_json(output / "matrix.json")["matrix"]["include"][0]
            self.assertEqual(matrix["concurrency_group"], "firmware-xiaomi-be6500")
            self.assertEqual(matrix["kernel_compression"], "default")
            for name in ("all", "s20p-mt76"):
                args.profile = name
                args.commit = None
                args.kernel_compression = "gzip"
                with self.subTest(profile=name), self.assertRaisesRegex(ValueError, "requires jdcloud"):
                    plan(args)


if __name__ == "__main__":
    unittest.main()
