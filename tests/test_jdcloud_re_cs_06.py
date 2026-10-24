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
    required_for, validate_config, validate_image_packages, validate_jdcloud_sysupgrade,
)
from Scripts.release import latest_jobs, release_title


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

JDCLOUD_DTS = """&sdhc {
	status = "okay";

	card@0 {
		compatible = "mmc-card";
		reg = <0>;

		block {
			compatible = "block-device";

			partitions {
				block-partition-factory {
					partname = "factory";

					nvmem-layout {
						compatible = "fixed-layout";
						#address-cells = <1>;
						#size-cells = <1>;

						lan_macaddr: macaddr@0 {
							compatible = "mac-base";
							reg = <0x0 0x6>;
							#nvmem-cell-cells = <1>;
						};

						wan_macaddr: macaddr@6 {
							compatible = "mac-base";
							reg = <0x6 0x6>;
							#nvmem-cell-cells = <1>;
						};
					};
				};
			};
		};
	};
};
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
        for key in self.profile["required"]:
            # Upstream defconfig supplies these unless the seed explicitly disables them.
            self.assertNotEqual(self.values.get(key), "n", key)
        final = dict(self.values)
        final.update({key: "y" for key in required_for(self.profile_id, self.profile, final)})
        final_text = "".join("CONFIG_" + key + "=" + value + "\n" for key, value in final.items())
        validate_config(self.profile_id, self.profile, final_text, final)
        for key in self.profile["forbidden"]:
            with self.subTest(forbidden=key), self.assertRaises(ValueError):
                validate_config(self.profile_id, self.profile, final_text + f"\nCONFIG_{key}=y\n", final)

    def test_s20p_plugin_selections_preserved(self):
        reference = merge_config([(ROOT / self.profiles["s20p-mt76"]["config"]).read_text()])
        plugins = {
            "PACKAGE_adguardhome", "PACKAGE_daed", "PACKAGE_honk", "PACKAGE_easytier",
            "PACKAGE_xray-core", "PACKAGE_v2ray-geoip", "PACKAGE_v2ray-geosite",
            "PACKAGE_dawn", "PACKAGE_umdns", "PACKAGE_batctl-default",
            "PACKAGE_kmod-batman-adv", "PACKAGE_MAC80211_MESH",
            "PACKAGE_luci-app-adguardhome", "PACKAGE_luci-app-autoreboot",
            "PACKAGE_luci-app-daede", "PACKAGE_luci-app-easytier",
            "PACKAGE_luci-app-honk", "PACKAGE_luci-app-mesh",
            "PACKAGE_luci-app-passwall", "PACKAGE_luci-app-upnp",
            "PACKAGE_luci-app-wolultra", "PACKAGE_luci-theme-shadcn",
        }
        plugins.update(key for key in reference if "_INCLUDE_" in key)
        for key in plugins:
            self.assertEqual(self.values.get(key), reference.get(key), key)
        for key in ("KERNEL_DEBUG_INFO_BTF", "DAED_USE_KERNEL_BTF", "BPF_TOOLCHAIN_HOST"):
            self.assertEqual(self.values[key], "y")

    def test_upstream_defaults_are_inherited_and_checked(self):
        inherited = {
            "autocore", "automount", "cpufreq", "e2fsprogs", "f2fs-tools", "losetup",
            "uboot-envtools", "wpad-openssl", "dnsmasq-full", "tc-tiny",
            "ath12k-firmware-ipq5332-ddwrt", "ath12k-firmware-qcn9274-ddwrt",
            "kmod-ath12k", "kmod-qcom-ppe",
            "kmod-qcom-qmi-helpers", "kmod-qrtr-mhi", "kmod-qrtr-smd", "kmod-dsa",
            "kmod-dsa-qca8k", "kmod-phy-aquantia", "kmod-phy-qca83xx",
            "kmod-pcs-qcom-ipq9574", "kmod-fs-ext4", "kmod-fs-f2fs",
            "kmod-gpio-button-hotplug", "kmod-leds-gpio", "kmod-usb3", "kmod-usb-dwc3",
            "kmod-usb-dwc3-qcom", "kmod-usb-serial-qualcomm", "kmod-usb-storage",
            "kmod-usb-storage-extras", "kmod-usb-storage-uas", "firewall4",
            "kmod-nf-flow", "kmod-nft-offload", "fullconenat-sonic",
        }
        gate_packages = set(self.profile["required_image_packages"])
        for name in inherited:
            self.assertNotIn("PACKAGE_" + name, self.values, name)
            self.assertIn(name, gate_packages, name)
        self.assertEqual(self.values["PACKAGE_ipq-wifi-jdcloud_re-cs-06"], "y")
        self.assertIn("ipq-wifi-jdcloud_re-cs-06", gate_packages)

    def test_proxy_and_offload_dependencies_preserved(self):
        for name in ("kmod-nf-flow", "kmod-nf-nat", "kmod-nft-nat", "kmod-nft-offload",
                     "fullconenat-sonic"):
            self.assertIn(name, self.profile["required_image_packages"], name)
        for name in ("kmod-sched", "kmod-tcp-bbr"):
            self.assertEqual(self.values["PACKAGE_" + name], "y", name)
            self.assertIn(name, self.profile["required_image_packages"], name)
        for key in ("PACKAGE_luci-app-passwall", "PACKAGE_xray-core", "PACKAGE_daed",
                    "PACKAGE_honk", "PACKAGE_easytier", "PACKAGE_adguardhome",
                    "PACKAGE_luci-app-mesh"):
            self.assertEqual(self.values[key], "y", key)
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

    def test_source_patches_fix_compression_and_emmc_mac_nvmem(self):
        before = ("define Device/xiaomi_be6500\n\t$(call Device/FitImage)\nendef\n\n" + JDCLOUD_DEVICE
                  + "\ndefine Device/jdcloud_re-cs-08\n\t$(call Device/FitImage)\nendef\n")
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            image = tree / "target/linux/qualcommbe/image/ipq53xx.mk"
            dts = tree / "target/linux/qualcommbe/dts/ipq5322-re-cs-06.dts"
            image.parent.mkdir(parents=True)
            dts.parent.mkdir(parents=True)
            image.write_text(before)
            dts.write_text(JDCLOUD_DTS)
            apply_source_patches(tree, compression_profile(self.profile_id, self.profile, "gzip"))
            self.assertEqual(image.read_text(), before)
            patched_dts = dts.read_text()
            self.assertIn('compatible = "gpt-partitions";', patched_dts)
            self.assertIn('partname = "0:ART";', patched_dts)
            self.assertNotIn('compatible = "block-device";', patched_dts)

            image.write_text(before)
            dts.write_text(JDCLOUD_DTS)
            apply_source_patches(tree, self.profile)
            expected = before.replace("define Device/jdcloud_re-cs-06\n\t$(call Device/FitImage)",
                                      "define Device/jdcloud_re-cs-06\n\t$(call Device/FitImageLzma)")
            self.assertEqual(image.read_text(), expected)
            self.assertIn('compatible = "gpt-partitions";', dts.read_text())
            self.assertIn('partname = "0:ART";', dts.read_text())

            image.write_text(before.replace("KERNEL_SIZE := 7168k", "KERNEL_SIZE := 8192k"))
            dts.write_text(JDCLOUD_DTS)
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
        self.assertIn("ImmortalWrt · JDCloud RE-CS-06", notes)
        self.assertNotIn("京东云无线宝赵云", notes)
        self.assertIn("LZMA/FIT", notes)
        self.assertIn("7 MiB", notes)
        self.assertIn(
            "RE-CS-06 使用 LZMA/FIT 内核，保留上游 7 MiB 内核区域与 eMMC 布局，刷入前请确认引导器兼容。",
            notes,
        )
        self.assertNotIn("不提供或自动更新引导器", notes)
        lock["profile"] = compression_profile(self.profile_id, self.profile, "gzip")
        notes = release_notes(lock)
        self.assertIn("GZIP/FIT", notes)
        self.assertIn("7 MiB", notes)
        self.assertNotIn("LZMA", notes)

    def test_readme_keeps_zhao_note_concise(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn(
            "> 赵云（构建选项 `jdcloud-re-cs-06`）使用 LZMA/FIT 内核，保留上游 7 MiB 内核区域与 eMMC 布局，刷入前请确认引导器兼容。",
            readme,
        )
        self.assertNotIn("### 赵云配置与压缩对比", readme)

    def test_compression_overrides_are_scoped_and_nonmutating(self):
        original = {**self.profile, "patches": list(self.profile["patches"])}
        self.assertEqual(compression_profile(self.profile_id, self.profile), self.profile)
        gzip = compression_profile(self.profile_id, self.profile, "gzip")
        self.assertEqual(gzip["kernel_compression"], "gzip")
        self.assertEqual(gzip["patches"], ["Scripts/Patches/jdcloud-re-cs-06-emmc-mac-nvmem.patch"])
        restored = compression_profile(self.profile_id, gzip, "lzma")
        self.assertEqual(restored, self.profile)
        self.assertEqual(original, self.profile)
        self.assertEqual(compression_profile(self.profile_id, self.profile, "lzma")["patches"], self.profile["patches"])
        extra = {**self.profile, "patches": [*self.profile["patches"], "Scripts/Patches/other.patch"]}
        self.assertEqual(compression_profile(self.profile_id, extra, "gzip")["patches"],
                         ["Scripts/Patches/jdcloud-re-cs-06-emmc-mac-nvmem.patch", "Scripts/Patches/other.patch"])
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
            self.assertEqual(len(locks["gzip"]["profile"]["patches"]), 1)
            self.assertTrue(locks["gzip"]["profile"]["patches"][0].endswith("emmc-mac-nvmem.patch"))
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


class ReleaseTests(unittest.TestCase):
    def test_release_title_uses_short_name_when_configured(self):
        profiles = read_toml(ROOT / "Config/profiles.toml")["profiles"]
        self.assertEqual(
            release_title("jdcloud-re-cs-06", profiles["jdcloud-re-cs-06"], "2026-10-24T01:50:33Z"),
            "JDCloud RE-CS-06",
        )
        self.assertEqual(
            release_title("x86-64", profiles["x86-64"], "2026-10-24T01:50:33Z"),
            "x86-64 · 2026-10-24",
        )

    def test_latest_jobs_accepts_compression_in_display_name(self):
        jobs = {"jobs": [
            {"id": 1, "name": "jdcloud-re-cs-06 / lzma / compile", "run_attempt": 1},
            {"id": 2, "name": "xiaomi-be6500 / default / compile", "run_attempt": 1},
            {"id": 3, "name": "jdcloud-re-cs-06 / publish", "run_attempt": 1},
            {"id": 4, "name": "build (x86-64, source) / compile", "run_attempt": 1},
            {"id": 5, "name": "x86-64 / compile", "run_attempt": 1},
            {"id": 6, "name": "plan", "run_attempt": 1},
        ]}
        with patch("Scripts.release.gh_json", return_value=[jobs]):
            parsed = latest_jobs("test/firmware", {"id": 1})
        self.assertEqual(set(parsed), {
            ("jdcloud-re-cs-06", "compile"),
            ("xiaomi-be6500", "compile"),
            ("jdcloud-re-cs-06", "publish"),
            ("x86-64", "compile"),
        })
        self.assertEqual(parsed[("jdcloud-re-cs-06", "compile")]["id"], 1)


if __name__ == "__main__":
    unittest.main()
