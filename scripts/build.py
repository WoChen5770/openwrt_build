#!/usr/bin/env python3
"""Plan, lock, integrate, validate and package builds; Python standard library only."""
import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def run(*args, cwd=None, env=None):
    try:
        return subprocess.check_output(args, cwd=cwd, env=env, text=True, timeout=600).strip()
    except subprocess.CalledProcessError as error:
        # Captured stdout often contains the useful feeds/make diagnostic.
        if error.output:
            print(error.output, flush=True)
        raise


def request(url):
    headers = {"User-Agent": "openwrt_build", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def resolve(repo, branch=None, commit=None):
    if commit:
        if not SHA.fullmatch(commit):
            raise ValueError("A pinned commit must be a full 40-character SHA")
        return commit
    ref = "refs/heads/" + branch if branch else "HEAD"
    lines = run("git", "ls-remote", repo, ref).splitlines()
    if len(lines) != 1 or not SHA.fullmatch(lines[0].split()[0]):
        raise ValueError("Cannot resolve source: " + repo + " " + ref)
    return lines[0].split()[0]


def parse_feeds(content):
    result = []
    for line in content.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3 or parts[0] not in ("src-git", "src-git-full"):
            raise ValueError("Unsupported feed declaration: " + line)
        kind, name, location = parts
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Invalid feed name")
        if "^" in location:
            repo, commit = location.split("^", 1)
            branch = None
        elif ";" in location:
            repo, branch = location.split(";", 1)
            commit = None
        else:
            repo, branch, commit = location, None, None
        if not repo.startswith("https://"):
            raise ValueError("Feeds must use HTTPS")
        result.append(dict(kind=kind, name=name, repository=repo, branch=branch, commit=commit))
    if len({f["name"] for f in result}) != len(result):
        raise ValueError("Duplicate feed names")
    return result


def lock_feed(feed):
    return {**feed, "commit": resolve(feed["repository"], feed["branch"], feed["commit"])}


def merge_config(fragments):
    values = {}
    for fragment in fragments:
        for line in fragment.splitlines():
            selected = re.fullmatch(r"CONFIG_([^=]+)=(.+)", line.strip())
            disabled = re.fullmatch(r"# CONFIG_(\S+) is not set", line.strip())
            if selected:
                values[selected[1]] = selected[2]
            elif disabled:
                values[disabled[1]] = "n"
    return values


def config_for(profile):
    return merge_config([(ROOT / path).read_text() for path in profile["fragments"]])


def required_for(profile_id, profile):
    values = config_for(profile)
    required = set(profile["required"])
    required.update(("TARGET_" + profile["target"], "TARGET_" + profile["target"] + "_" + profile["subtarget"],
                     "TARGET_DEVICE_" + profile["target"] + "_" + profile["subtarget"] + "_DEVICE_" + profile["device"],
                     "PACKAGE_luci", "PACKAGE_luci-ssl-openssl", "PACKAGE_luci-theme-shadcn"))
    # Guard user-visible features, rather than allowing defconfig to silently drop them.
    for option in ("PACKAGE_luci-app-mesh", "PACKAGE_kmod-batman-adv", "PACKAGE_wpad-openssl",
                   "PACKAGE_MAC80211_MESH", "PACKAGE_dawn", "PACKAGE_umdns", "PACKAGE_luci-app-passwall",
                   "PACKAGE_xray-core", "PACKAGE_luci-app-passwall_INCLUDE_Xray",
                   "PACKAGE_luci-app-daede", "PACKAGE_daed", "PACKAGE_honk",
                   "PACKAGE_luci-app-honk", "PACKAGE_luci-i18n-honk-zh-cn", "PACKAGE_luci-app-easytier",
                   "PACKAGE_easytier", "PACKAGE_adguardhome", "PACKAGE_luci-app-adguardhome",
                   "KERNEL_DEBUG_INFO_BTF", "DAED_USE_KERNEL_BTF", "BPF_TOOLCHAIN_HOST"):
        if values.get(option) == "y":
            required.add(option)
    if profile_id == "x86-64":
        required.update(("PACKAGE_kmod-tcp-bbr", "PACKAGE_kmod-sched", "PACKAGE_tc-full"))
    if profile_id == "s20p-mtk":
        required.update(("PACKAGE_luci-app-eqos-mtk", "PACKAGE_luci-app-turboacc-mtk"))
    return required


def validate_config(profile_id, profile, content):
    values = merge_config([content])
    errors = ["Missing CONFIG_" + key + "=y" for key in sorted(required_for(profile_id, profile)) if values.get(key) != "y"]
    # Vendor drivers have tristate/string/int platform settings, not just packages.
    for key, expected_value in profile.get("expected_config", {}).items():
        if values.get(key) != expected_value:
            errors.append("Expected CONFIG_" + key + "=" + expected_value + "; got " + values.get(key, "<missing>"))
    forbidden = set(profile["forbidden"]) | {"PACKAGE_bandix", "PACKAGE_luci-app-bandix", "PACKAGE_wpad-mesh-openssl"}
    # Preserve exclusions after merging device and shared feature fragments.
    forbidden.update(key for key, value in config_for(profile).items() if key.startswith("PACKAGE_") and value == "n")
    if values.get("PACKAGE_luci-app-passwall") == "y":
        # Fail closed if upstream adds a new, enabled proxy backend.
        allowed = {"PACKAGE_luci-app-passwall_INCLUDE_Xray", "PACKAGE_luci-app-passwall_INCLUDE_Geoview",
                   "PACKAGE_luci-app-passwall_INCLUDE_V2ray_Geodata"}
        forbidden.update(key for key in values if key.startswith("PACKAGE_luci-app-passwall_INCLUDE_") and key not in allowed)
    errors += ["Forbidden CONFIG_" + key for key in sorted(forbidden) if values.get(key) in ("y", "m")]
    expected = "TARGET_DEVICE_" + profile["target"] + "_" + profile["subtarget"] + "_DEVICE_" + profile["device"]
    if any(key.startswith("TARGET_DEVICE_") and value == "y" and key != expected for key, value in values.items()):
        errors.append("More than one device selected")
    if values.get("TARGET_ALL_PROFILES") == "y":
        errors.append("All-device builds are not allowed")
    if errors:
        raise ValueError("\n".join(errors))


def defaults(profile_id=None):
    values = read_json(ROOT / "firmware-defaults.json")
    profiles = read_json(ROOT / "profiles.json")["profiles"]
    for profile in profiles.values():
        hostname = profile["hostname"]
        if not isinstance(hostname, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", hostname):
            raise ValueError("Invalid model hostname")
    if profile_id is not None:
        values["hostname"] = profiles[profile_id]["hostname"]
    if ipaddress.ip_address(values["lan_ip"]).version != 4:
        raise ValueError("LAN IP must be IPv4")
    # Avoid shell/UCI interpolation and Makefile injection; defaults are public.
    for key in ("wifi_ssid", "wifi_password", "theme", "timezone", "zonename"):
        if not isinstance(values[key], str) or not re.fullmatch(r"[A-Za-z0-9_./+ :@-]+", values[key]):
            raise ValueError("Unsupported characters in " + key)
    if not 1 <= len(values["wifi_ssid"].encode()) <= 32 or not 8 <= len(values["wifi_password"]) <= 63:
        raise ValueError("Invalid SSID or WPA password length")
    if values["wifi_encryption"] not in ("psk2", "psk2+ccmp", "sae-mixed"):
        raise ValueError("Unsupported Wi-Fi encryption")
    if not isinstance(values["wifi_disabled"], bool):
        raise ValueError("wifi_disabled must be boolean")
    pw = values.get("root_password_hash", "")
    if pw and (not pw.startswith(("$5$", "$6$")) or re.search(r"[\s:'\"]", pw)):
        raise ValueError("Use a SHA256/SHA512 crypt root_password_hash, not plaintext")
    return values


def ci_hash():
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(part in (".git", ".state", "out", "logs", "__pycache__", "openwrt", "tests") for part in rel.parts):
            continue
        digest.update(str(rel).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def xray_lock(profile_id):
    asset_name = "Xray-linux-64.zip" if profile_id == "x86-64" else "Xray-linux-arm64-v8a.zip"
    releases = json.loads(request("https://api.github.com/repos/XTLS/Xray-core/releases?per_page=100"))
    release = max((r for r in releases if not r["draft"] and r.get("published_at")), key=lambda r: r["published_at"])
    asset = next((a for a in release["assets"] if a["name"] == asset_name and a["state"] == "uploaded"), None)
    if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
        raise ValueError("Latest Xray asset is absent or has no SHA256; refusing an older/unchecked binary")
    tag = release["tag_name"]
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", tag):
        raise ValueError("Invalid Xray tag")
    return dict(tag=tag, version=tag.removeprefix("v"), asset=asset_name, asset_id=asset["id"], sha256=asset["digest"][7:])


def source_marker(source):
    identity = {key: source[key] for key in ("repository", "branch", "commit")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return "<!-- build-source:" + digest + " -->"


def plan(args):
    settings = read_json(ROOT / "build-settings.json")
    matrix = []
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    specs = read_json(ROOT / "profiles.json")
    if args.profile != "all" and args.profile not in specs["profiles"]:
        raise ValueError("Unknown profile")
    if args.commit and args.profile == "all":
        raise ValueError("A commit override requires a single profile")
    if args.event != "schedule" or settings["scheduled_builds"]:
        defaults()
        known = []
        if args.event == "schedule" or not args.force:
            pages = json.loads(run("gh", "api", "repos/" + os.environ["GITHUB_REPOSITORY"] + "/releases?per_page=100", "--paginate", "--slurp"))
            known = [r for page in pages for r in page if not r["draft"]]
        packages = read_json(ROOT / "packages.json")
        sources = {}
        source_commits = {}
        package_commits = {}
        code_hash = ci_hash()
        for profile_id, profile in specs["profiles"].items():
            if args.profile not in ("all", profile_id):
                continue
            source_id = profile["source"]
            source = specs["sources"][source_id]
            if source_id not in source_commits:
                source_commits[source_id] = resolve(source["repository"], source["branch"], args.commit)
            current_source = {**source, "commit": source_commits[source_id]}
            if args.event == "schedule":
                # Releases arrive newest first. Compare the latest successful version per device.
                latest = next((r for r in known if r["tag_name"].startswith(profile_id + "-")), None)
                if latest and source_marker(current_source) in (latest.get("body") or ""):
                    print("Source unchanged: " + profile_id)
                    continue
            if source_id not in sources:
                commit = source_commits[source_id]
                slug = source["repository"].removeprefix("https://github.com/").removesuffix(".git")
                content = request("https://raw.githubusercontent.com/" + slug + "/" + commit + "/feeds.conf.default").decode()
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    feeds = list(pool.map(lock_feed, parse_feeds(content)))
                sources[source_id] = dict(**source, commit=commit, feeds=feeds)
            selected = {}
            for name, package in packages.items():
                if "all" not in package["profiles"] and profile_id not in package["profiles"]:
                    continue
                key = (package["repository"], package.get("branch"), package.get("commit"))
                if key not in package_commits:
                    package_commits[key] = resolve(*key)
                selected[name] = {**package, "commit": package_commits[key]}
            lock = dict(profile_id=profile_id, profile=profile, source=sources[source_id], packages=selected,
                        ci_hash=code_hash, defaults=defaults(profile_id))
            if config_for(profile).get("PACKAGE_xray-core") == "y":
                lock["xray"] = xray_lock(profile_id)
            lock["fingerprint"] = hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()
            marker = "<!-- build-fingerprint:" + lock["fingerprint"] + " -->"
            if args.event != "schedule" and any(r["tag_name"].startswith(profile_id + "-") and marker in (r.get("body") or "") for r in known):
                print("Unchanged: " + profile_id)
                continue
            write_json(output / (profile_id + ".json"), lock)
            matrix.append(dict(profile=profile_id, source_commit=lock["source"]["commit"], fingerprint=lock["fingerprint"]))
    result = dict(matrix=dict(include=matrix), has_builds=bool(matrix), max_parallel=settings["max_parallel"],
                  retention_days=settings["artifact_retention_days"])
    write_json(output / "matrix.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if args.github_output:
        with open(args.github_output, "a") as stream:
            for key, value in result.items():
                stream.write(key + "=" + json.dumps(value, separators=(",", ":")) + "\n")


def checkout(repo, commit, dest):
    dest.mkdir(parents=True)
    run("git", "init", "-q", str(dest))
    run("git", "-C", str(dest), "remote", "add", "origin", repo)
    run("git", "-C", str(dest), "fetch", "--depth=1", "origin", commit)
    run("git", "-C", str(dest), "checkout", "-q", "--detach", "FETCH_HEAD")
    if run("git", "-C", str(dest), "rev-parse", "HEAD") != commit:
        raise ValueError("Source checkout differs from build plan")


def remove_packages(tree, names):
    # Only exact-name recipe directories/links inside this disposable source tree.
    for root in (tree / "package", tree / "feeds/luci", tree / "feeds/packages"):
        for current, dirs, _ in os.walk(root, followlinks=False):
            path = Path(current)
            if len(path.relative_to(root).parts) >= 3:
                dirs[:] = []
                continue
            for name in list(dirs):
                if name not in names:
                    continue
                dest = path / name
                if dest.is_symlink():
                    dest.unlink()
                else:
                    shutil.rmtree(dest)
                dirs.remove(name)


def integrate(lock, tree):
    env = {**os.environ, "GITHUB_WORKSPACE": str(ROOT), "WRT_CONFIG": lock["profile"]["legacy_name"]}
    for name, package in lock["packages"].items():
        mode = package["mode"]
        if mode in ("mesh", "honk"):
            if mode == "mesh":
                env["MESH_COMMIT"] = package["commit"]
            else:
                env["HONK_COMMIT"] = package["commit"]
            run("bash", str(ROOT / "legacy" / ("Mesh.sh" if mode == "mesh" else "Honk.sh")), cwd=tree, env=env)
            continue
        with tempfile.TemporaryDirectory(prefix="owrt-package-") as temporary:
            clone = Path(temporary) / "source"
            checkout(package["repository"], package["commit"], clone)
            dirs = package.get("directories") or (["."] if (clone / "Makefile").exists() else
                                                      [p.name for p in clone.iterdir() if p.is_dir() and (p / "Makefile").exists()])
            for directory in dirs:
                if not (clone / directory / "Makefile").is_file():
                    raise ValueError(name + ": missing recipe " + directory)
            names = package.get("remove") or ([name] if dirs == ["."] else dirs)
            remove_packages(tree, names)
            if mode == "whole":
                destination = tree / "package/custom" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(clone, destination, ignore=shutil.ignore_patterns(".git", ".github"))
            else:
                for directory in dirs:
                    shutil.copytree(clone / directory, tree / "package" / directory, ignore=shutil.ignore_patterns(".git", ".github"))
    if "xray" in lock:
        xray = lock["xray"]
        remove_packages(tree, ["xray-core"])
        target = tree / "package/xray-core"
        target.mkdir()
        template = (ROOT / "legacy/Makefiles/xray-core-prebuilt.mk").read_text()
        replacements = dict(XRAY_VERSION=xray["version"], XRAY_TAG=xray["tag"], XRAY_ASSET=xray["asset"],
                            XRAY_SOURCE=xray["asset"].removesuffix(".zip") + "-" + xray["version"] + "-" + str(xray["asset_id"]) + ".zip",
                            XRAY_HASH=xray["sha256"], XRAY_ARCH_DEPENDS="@x86_64" if lock["profile_id"] == "x86-64" else "@aarch64",
                            XRAY_ARCH_LABEL="x86-64" if lock["profile_id"] == "x86-64" else "ARM64")
        for key, value in replacements.items():
            template = template.replace("@" + key + "@", value)
        (target / "Makefile").write_text(template)
    # Keep the old optional updater recipes without selecting them by default.
    for name in ("upx-arm64-static", "owrt-core-update"):
        target = tree / "package" / name
        target.mkdir(exist_ok=True)
        shutil.copy2(ROOT / "legacy/Makefiles" / (name + ".mk"), target / "Makefile")
    target = tree / "package/owrt-core-update/files"
    target.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "legacy/Files/core-update/owrt-core-update", target / "owrt-core-update")
    # Maintain the old Rust build fix without unrelated theme/download hacks.
    for path in (tree / "feeds/packages").glob("*/rust/Makefile"):
        path.write_text(path.read_text().replace("ci-llvm=true", "ci-llvm=false"))


def apply_defaults(tree, values, vendor=False):
    generator = tree / "package/base-files/files/bin/config_generate"
    content, count = re.subn(r"192\.168\.\d+\.\d+", values["lan_ip"], generator.read_text())
    if not count:
        raise ValueError("LAN default generator changed upstream")
    content, count = re.subn(r"hostname='[^']*'", "hostname='" + values["hostname"] + "'", content)
    if not count:
        raise ValueError("Hostname default generator changed upstream")
    generator.write_text(content)
    if vendor:
        path = tree / "package/mtk/applications/mtwifi-cfg-ucode/files/lib/wifi/mtwifi.uc"
        content = path.read_text()
        for anchor, value in ((r'"ssid": defs\.ssid,', '"ssid": ' + json.dumps(values["wifi_ssid"]) + ','),
                              (r'"encryption": "none"', '"encryption": ' + json.dumps(values["wifi_encryption"]) +
                               ',\n        "key": ' + json.dumps(values["wifi_password"]) +
                               ',\n        "disabled": "' + str(int(values["wifi_disabled"])) + '"')):
            content, count = re.subn(anchor, lambda _: value, content)
            if count != 1:
                raise ValueError("Vendor Wi-Fi defaults changed upstream")
        path.write_text(content)
    else:
        path = tree / "package/network/config/wifi-scripts/files/lib/wifi/mac80211.uc"
        content = path.read_text()
        for key, value in dict(ssid=values["wifi_ssid"], key=values["wifi_password"],
                               encryption=values["wifi_encryption"], disabled=str(int(values["wifi_disabled"]))).items():
            content, count = re.subn(r"^(set \$\{si\}\." + key + r"=).*$", lambda m: m[1] + "'" + value + "'", content, flags=re.M)
            if count != 1:
                raise ValueError("mac80211 " + key + " defaults changed upstream")
        path.write_text(content)
    overlay = tree / "files/etc/uci-defaults"
    overlay.mkdir(parents=True, exist_ok=True)
    for original in (ROOT / "legacy/uci-defaults").iterdir():
        shutil.copy2(original, overlay / original.name)
    # Only system settings; never overwrite restored wireless/network configuration.
    script = "#!/bin/sh\nuci set system.@system[0].hostname='" + values["hostname"] + "'\n"
    script += "uci set system.@system[0].timezone='" + values["timezone"] + "'\n"
    script += "uci set system.@system[0].zonename='" + values["zonename"] + "'\nuci commit system\n"
    script += "uci set luci.main.mediaurlbase='/luci-static/" + values["theme"] + "'\nuci commit luci\nexit 0\n"
    (overlay / "95-project-defaults").write_text(script)
    for path in overlay.iterdir():
        path.chmod(0o755)
    password = values.get("root_password_hash")
    if password:
        path = tree / "package/base-files/files/etc/shadow"
        content, count = re.subn(r"^root:[^:]*:", lambda _: "root:" + password + ":", path.read_text(), flags=re.M)
        if count != 1:
            raise ValueError("Cannot set root password hash")
        path.write_text(content)
    if vendor:
        # Preinstalled experimental proxy/Mesh tools stay disabled on first boot.
        script = "#!/bin/sh\n"
        for service in ("daed", "honk", "mesh"):
            script += "[ ! -x /etc/init.d/" + service + " ] || /etc/init.d/" + service + " disable\n"
        script += "uci -q set mesh.main.enabled='0'\nuci -q set mesh.main.button='0'\nuci -q commit mesh\n"
        script += "uci -q set daed.config.enabled='0'\nuci -q commit daed\nexit 0\n"
        path = overlay / "98-mtk-experimental-off"
        path.write_text(script)
        path.chmod(0o755)


def prepare(args):
    lock = read_json(args.lock)
    if lock["ci_hash"] != ci_hash():
        raise ValueError("CI files changed since planning")
    tree = Path(args.source).resolve()
    if run("git", "-C", str(tree), "rev-parse", "HEAD") != lock["source"]["commit"]:
        raise ValueError("Upstream SHA does not match plan")
    (tree / "feeds.conf").write_text("".join("src-git-full " + f["name"] + " " + f["repository"] + "^" + f["commit"] + "\n" for f in lock["source"]["feeds"]))
    run("./scripts/feeds", "update", "-a", cwd=tree)
    for feed in lock["source"]["feeds"]:
        actual = run("git", "-C", str(tree / "feeds" / feed["name"]), "rev-parse", "HEAD")
        if actual != feed["commit"]:
            raise ValueError("Feed checkout mismatch: " + feed["name"])
    run("./scripts/feeds", "install", "-a", cwd=tree)
    integrate(lock, tree)
    apply_defaults(tree, lock["defaults"], lock["profile"]["source"] == "chasey")
    values = config_for(lock["profile"])
    content = "".join("CONFIG_" + key + "=" + value + "\n" for key, value in values.items())
    (tree / ".config").write_text(content)
    run("make", "defconfig", cwd=tree)
    validate_config(lock["profile_id"], lock["profile"], (tree / ".config").read_text())
    print("Configuration verified: " + lock["profile_id"])


def validate_image_packages(target, profile):
    required = set(profile.get("required_image_packages", []))
    if not required:
        return
    manifests = list(target.glob("*" + profile["image_match"] + "*.manifest"))
    if len(manifests) != 1:
        raise ValueError("Expected one device package manifest")
    installed = {line.split()[0] for line in manifests[0].read_text().splitlines() if line.strip()}
    missing = sorted(required - installed)
    if missing:
        raise ValueError("Firmware is missing packages: " + ", ".join(missing))


def validate_mtk_sysupgrade(path):
    # Inspect the actual SquashFS, not just Kconfig or the package manifest.
    with tempfile.TemporaryDirectory(prefix="owrt-image-check-") as temporary:
        root = Path(temporary) / "root.squashfs"
        with tarfile.open(path) as archive:
            members = [m for m in archive.getmembers() if m.isfile() and m.name.endswith("/root")]
            if len(members) != 1:
                raise ValueError("Expected one sysupgrade root filesystem")
            with archive.extractfile(members[0]) as source, root.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        listing = run("unsquashfs", "-ll", str(root))
    files = {line.split()[-1].removeprefix("squashfs-root/")
             for line in listing.splitlines() if line.startswith("-")}
    required = {
        "lib/wifi/mtwifi.uc", "lib/wifi/mtwifi.sh", "lib/netifd/wireless/mtwifi.sh",
        "usr/share/ucode/mtwifi/driver.uc", "usr/lib/ucode/datconf.so",
        "usr/lib/ucode/l1parser.so", "usr/lib/ucode/iwinfo.so",
        "usr/share/luci-app-mtwifi-cfg/wireless-mtk.js", "etc/modules.d/mt_wifi",
        "lib/firmware/WIFI_RAM_CODE_MT7986.bin",
    }
    missing = sorted(required - files)
    for module in ("mt_wifi.ko", "conninfra.ko", "mtk_warp.ko"):
        if not any(p.startswith("lib/modules/") and p.endswith("/" + module) for p in files):
            missing.append(module)
    if missing:
        raise ValueError("MTK wireless files missing from image: " + ", ".join(missing))


def release_notes(lock):
    profile, values, source = lock["profile"], lock["defaults"], lock["source"]
    label = profile.get("display_name", lock["profile_id"])
    repository = source["repository"].removesuffix(".git")
    notes = "ImmortalWrt · " + label + "\n\n"
    notes += "### 开始使用\n\n| 项目 | 默认设置 |\n| :--- | :--- |\n"
    notes += "| 管理入口 | `" + values["lan_ip"] + "` · HTTP / HTTPS |\n"
    notes += "| 登录 | `root` · " + ("已设密码" if values.get("root_password_hash") else "空密码") + " |\n"
    notes += "| 界面 | `" + values["theme"] + "` · 简体中文 |\n"
    notes += "| 主机名 | `" + values["hostname"] + "` |\n"
    if profile["target"] != "x86":
        notes += "| Wi-Fi | `" + values["wifi_ssid"] + "` · `" + values["wifi_password"] + "` |\n"
    else:
        notes += "\nx86 无内置无线时不生成热点。\n"
    notes += "\n> 首次登录请修改默认密码。只刷匹配机型与分区的镜像；sysupgrade 不代表适用于原厂首次刷入，勿强刷。\n"
    if lock["profile_id"].startswith("s20p-"):
        notes += "\n> S20P MTK / MT76 不保证直接互刷。"
        if profile["source"] == "chasey":
            notes += "Mesh / daed / honk 默认关闭；厂商无线的 802.11s 未验证，HNAT / WARP 与透明代理需验证兼容性。"
        notes += "\n"
    notes += "\n<details>\n<summary>源码与文件</summary>\n\n"
    notes += "源码：[" + source["branch"] + " · " + source["commit"][:7] + "](" + repository + "/commit/" + source["commit"] + ")。\n\n"
    notes += "校验：`sha256sums` · 配置：`config.full` / `config.diff` · 构建记录：`build-info.json`。\n\n"
    notes += "`packages.tar.gz` 为同次构建的软件包归档，不是在线软件源；内核模块须匹配 ABI。\n\n</details>\n\n"
    notes += "<!-- build-fingerprint:" + lock["fingerprint"] + " -->\n"
    return notes + source_marker(source) + "\n"


def collect(args):
    lock = read_json(args.lock)
    tree = Path(args.source)
    output = Path(args.output_dir)
    profile = lock["profile"]
    target = tree / "bin/targets" / profile["target"] / profile["subtarget"]
    validate_config(lock["profile_id"], profile, (tree / ".config").read_text())
    validate_image_packages(target, profile)
    if lock["profile_id"] == "s20p-mtk":
        sysupgrades = list(target.glob("*" + profile["image_match"] + "*sysupgrade*.bin"))
        if len(sysupgrades) != 1:
            raise ValueError("Expected one S20P MTK sysupgrade image")
        validate_mtk_sysupgrade(sysupgrades[0])
    output.mkdir(parents=True, exist_ok=True)
    images = []
    for path in sorted(target.iterdir()):
        if not path.is_file():
            continue
        is_image = profile["image_match"] in path.name and (
            any(s in path.name for s in ("sysupgrade", "factory", "initramfs")) or
            (profile["target"] == "x86" and re.search(r"\.(img(?:\.gz)?|vmdk|vdi|qcow2)$", path.name)))
        if is_image:
            shutil.copy2(path, output / path.name)
            images.append(path.name)
        elif (path.suffix == ".manifest" and profile["image_match"] in path.name) or path.name.endswith((".buildinfo", ".json")):
            shutil.copy2(path, output / path.name)
    if not images:
        raise ValueError("No matching firmware images produced")
    shutil.copy2(tree / ".config", output / "config.full")
    (output / "config.seed").write_text("".join((ROOT / p).read_text() + "\n" for p in profile["fragments"]))
    (output / "config.diff").write_text(run("./scripts/diffconfig.sh", cwd=tree) + "\n")
    with tarfile.open(output / "packages.tar.gz", "w:gz") as archive:
        for path in (target / "packages", tree / "bin/packages"):
            if path.is_dir():
                archive.add(path, arcname=str(path.relative_to(tree / "bin")))
    info = {**lock, "ci_commit": os.environ.get("GITHUB_SHA", "local"), "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "images": images, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    write_json(output / "build-info.json", info)
    (output / "release-notes.md").write_text(release_notes(lock))
    checksums = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "sha256sums":
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            checksums.append(digest.hexdigest() + "  " + path.name + "\n")
    (output / "sha256sums").write_text("".join(checksums))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--profile", default="all")
    p.add_argument("--commit")
    p.add_argument("--event", default="workflow_dispatch")
    p.add_argument("--force", action="store_true")
    p.add_argument("--output-dir", default=".state")
    p.add_argument("--github-output")
    for name in ("prepare", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True)
        p.add_argument("--lock", required=True)
        if name == "collect":
            p.add_argument("--output-dir", default="out")
    args = parser.parse_args()
    {"plan": plan, "prepare": prepare, "collect": collect}[args.command](args)


if __name__ == "__main__":
    main()
