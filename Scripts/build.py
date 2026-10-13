#!/usr/bin/env python3
"""Plan, lock, integrate, validate and package builds; Python 3.11+ standard library."""
import argparse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")


def read_json(path):
    return json.loads(Path(path).read_text())


def read_toml(path):
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)


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


def config_for(profile, packages=""):
    selections = [(ROOT / profile["config"]).read_text()]
    private = ROOT / "Config/PRIVATE.txt"
    if private.is_file():
        selections.append(private.read_text())
    selections.append(package_overrides(packages))
    return merge_config(selections)


def package_overrides(content):
    # Same PACKAGE input as OpenWRT-CI, but only package selections, never shell.
    content = content.replace("\\n", "\n")
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# CONFIG_") and not re.fullmatch(r"# CONFIG_PACKAGE_[A-Za-z0-9_+-]+ is not set", line):
            raise ValueError("PACKAGE accepts only package selections")
        if line and not line.startswith("#") and not re.fullmatch(r"CONFIG_PACKAGE_[A-Za-z0-9_+-]+=[ymn]", line):
            raise ValueError("PACKAGE accepts only CONFIG_PACKAGE_name=y/m/n lines")
    return content


def required_for(profile_id, profile, values=None):
    values = config_for(profile) if values is None else values
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


def validate_config(profile_id, profile, content, selections=None):
    values = merge_config([content])
    selections = config_for(profile) if selections is None else selections
    errors = ["Missing CONFIG_" + key + "=y" for key in sorted(required_for(profile_id, profile, selections)) if values.get(key) != "y"]
    # Vendor drivers have tristate/string/int platform settings, not just packages.
    for key, expected_value in profile.get("expected_config", {}).items():
        if values.get(key) != expected_value:
            errors.append("Expected CONFIG_" + key + "=" + expected_value + "; got " + values.get(key, "<missing>"))
    forbidden = set(profile["forbidden"]) | {"PACKAGE_bandix", "PACKAGE_luci-app-bandix", "PACKAGE_wpad-mesh-openssl"}
    # Preserve exclusions after applying private and temporary package overrides.
    forbidden.update(key for key, value in selections.items() if key.startswith("PACKAGE_") and value == "n")
    if values.get("PACKAGE_luci-app-passwall") == "y":
        # Fail closed if upstream adds a new, enabled proxy backend.
        allowed = {"PACKAGE_luci-app-passwall_INCLUDE_Xray", "PACKAGE_luci-app-passwall_INCLUDE_Geoview",
                   "PACKAGE_luci-app-passwall_INCLUDE_V2ray_Geodata"}
        forbidden.update(key for key in values if key.startswith("PACKAGE_luci-app-passwall_INCLUDE_") and key not in allowed)
    errors += ["Forbidden CONFIG_" + key for key in sorted(forbidden) if values.get(key) in ("y", "m")]
    errors += ["Forbidden QModem option: CONFIG_" + key for key, value in values.items()
               if value in ("y", "m") and key.lower().startswith(("package_qmodem", "package_luci-app-qmodem", "package_luci-i18n-qmodem"))]
    expected = "TARGET_DEVICE_" + profile["target"] + "_" + profile["subtarget"] + "_DEVICE_" + profile["device"]
    if any(key.startswith("TARGET_DEVICE_") and value == "y" and key != expected for key, value in values.items()):
        errors.append("More than one device selected")
    if values.get("TARGET_ALL_PROFILES") == "y":
        errors.append("All-device builds are not allowed")
    if errors:
        raise ValueError("\n".join(errors))


def defaults(profile_id=None):
    values = read_toml(ROOT / "Config/firmware-defaults.toml")
    profiles = read_toml(ROOT / "Config/profiles.toml")["profiles"]
    if profile_id is not None:
        values["hostname"] = profiles[profile_id]["hostname"]
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


def config_marker(values):
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return "<!-- build-config:" + digest + " -->"


def plan(args):
    settings = read_toml(ROOT / "Config/build-settings.toml")
    matrix = []
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    specs = read_toml(ROOT / "Config/profiles.toml")
    if args.profile != "all" and args.profile not in specs["profiles"]:
        raise ValueError("Unknown profile")
    if args.commit and args.profile == "all":
        raise ValueError("A commit override requires a single profile")
    overrides = package_overrides(getattr(args, "packages", ""))
    owner = os.environ.get("GITHUB_REPOSITORY", "local/local").split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ValueError("Invalid repository owner")
    if args.event != "schedule" or settings["scheduled_builds"]:
        defaults()
        known = []
        if args.event == "schedule" or not args.force:
            pages = json.loads(run("gh", "api", "repos/" + os.environ["GITHUB_REPOSITORY"] + "/releases?per_page=100", "--paginate", "--slurp"))
            known = [r for page in pages for r in page if not r["draft"]]
        packages = read_toml(ROOT / "Config/packages.toml")
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
            if args.event == "schedule" and not overrides:
                # Releases arrive newest first. Compare the latest successful version per device.
                latest = next((r for r in known if r["tag_name"].startswith(profile_id + "-")), None)
                if (latest and source_marker(current_source) in (latest.get("body") or "")
                        and "<!-- build-ci:" + code_hash + " -->" in (latest.get("body") or "")
                        and config_marker(config_for(profile)) in (latest.get("body") or "")):
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
                        ci_hash=code_hash, defaults=defaults(profile_id), config=config_for(profile, overrides),
                        repository_owner=owner)
            private = ROOT / "Config/PRIVATE.txt"
            lock["requested_packages"] = merge_config([private.read_text() if private.is_file() else "", overrides])
            if lock["config"].get("PACKAGE_xray-core") == "y":
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
            run("bash", str(ROOT / "Scripts" / ("Mesh.sh" if mode == "mesh" else "Honk.sh")), cwd=tree, env=env)
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
    if "luci-app-adguardhome" in lock["packages"]:
        use_packaged_adguardhome(tree)
    if "xray" in lock:
        xray = lock["xray"]
        remove_packages(tree, ["xray-core"])
        target = tree / "package/xray-core"
        target.mkdir()
        template = (ROOT / "Scripts/Makefiles/xray-core-prebuilt.mk").read_text()
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
        shutil.copy2(ROOT / "Scripts/Makefiles" / (name + ".mk"), target / "Makefile")
    target = tree / "package/owrt-core-update/files"
    target.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "Scripts/Files/core-update/owrt-core-update", target / "owrt-core-update")
    # Maintain the old Rust build fix without unrelated theme/download hacks.
    for path in (tree / "feeds/packages").glob("*/rust/Makefile"):
        path.write_text(path.read_text().replace("ci-llvm=true", "ci-llvm=false"))


def use_packaged_adguardhome(tree):
    # Use the upstream core without changing its version, Go or service files.
    path = tree / "package/custom/luci-app-adguardhome/root/etc/config/AdGuardHome"
    content, count = re.subn(r"(?m)^([ \t]*option[ \t]+binpath[ \t]+)'[^'\n]*'[ \t]*$",
                            lambda m: m[1] + "'/usr/bin/AdGuardHome'", path.read_text())
    if count != 1:
        raise ValueError("AdGuard Home binary path defaults changed upstream")
    path.write_text(content)


def apply_kernel_config(tree, profile):
    """Scoped platform BPF settings required by the 7DR7299 reference builds."""
    specification = profile.get("kernel_config")
    if not specification:
        return
    relative = Path(specification["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("target", "linux"):
        raise ValueError("Invalid kernel config path")
    path = tree / relative
    if not path.is_file():
        raise ValueError("Kernel config changed upstream: " + str(relative))
    options = specification["options"]
    if any(not re.fullmatch(r"[A-Z0-9_]+", key) or value not in ("y", "m", "n") for key, value in options.items()):
        raise ValueError("Invalid kernel config option")
    lines = []
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"(?:CONFIG_([^=]+)=.*|# CONFIG_(\S+) is not set)", line)
        if not match or (match[1] or match[2]) not in options:
            lines.append(line)
    lines.extend("CONFIG_" + key + "=" + value for key, value in sorted(options.items()))
    path.write_text("\n".join(lines) + "\n")


def apply_defaults(tree, values, vendor=False, wireless_defaults=None):
    generator = tree / "package/base-files/files/bin/config_generate"
    content, count = re.subn(r"192\.168\.\d+\.\d+", values["lan_ip"], generator.read_text())
    if not count:
        raise ValueError("LAN default generator changed upstream")
    content, count = re.subn(r"hostname='[^']*'", "hostname='" + values["hostname"] + "'", content)
    if not count:
        raise ValueError("Hostname default generator changed upstream")
    generator.write_text(content)
    if vendor and wireless_defaults == "mtwifi-shell":
        path = tree / "package/mtk/applications/mtwifi-cfg/files/mtwifi.sh"
        content = path.read_text()
        for key, value in (("ssid", "'" + values["wifi_ssid"] + "'"),
                           ("encryption", "'" + values["wifi_encryption"] + "'")):
            pattern = r"(?m)^([ \t]*set wireless\.default_\$\{dev\}\." + key + r"=).*$"
            replacement = lambda m: m[1] + value
            if key == "encryption":
                def replacement(match):
                    indent = re.match(r"[ \t]*", match[1])[0]
                    return (match[1] + value + "\n" + indent + "set wireless.default_${dev}.key='" +
                            values["wifi_password"] + "'\n" + indent + "set wireless.default_${dev}.disabled='" +
                            str(int(values["wifi_disabled"])) + "'")
            content, count = re.subn(pattern, replacement, content)
            if count != 1:
                raise ValueError("Vendor shell Wi-Fi " + key + " defaults changed upstream")
        path.write_text(content)
    elif vendor:
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
    for original in (ROOT / "Scripts/uci-defaults").iterdir():
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
        # Set packaged defaults, not restored UCI state. The init scripts respect
        # these switches, so a kept-config sysupgrade must not disable services.
        for relative, options in (
            ("package/daed/files/daed.config", ("enabled",)),
            ("package/honk/files/honk.config", ("enabled",)),
        ):
            path = tree / relative
            content = path.read_text()
            for option in options:
                pattern = r"(?m)^([ \t]*option[ \t]+" + option + r"[ \t]+)(?:'[^'\n]*'|\"[^\"\n]*\"|\S+)[ \t]*$"
                content, count = re.subn(pattern, lambda m: m[1] + "'0'", content)
                if count != 1:
                    raise ValueError("Service defaults changed upstream: " + relative + " / " + option)
            path.write_text(content)


def remove_qmodem(tree):
    """Prune matching recipes and feed links without following directory symlinks."""
    for root in (tree / "package", tree / "feeds"):
        for current, directories, files in os.walk(root, followlinks=False):
            for name in directories[:] + files:
                if not name.lower().startswith(("qmodem", "luci-app-qmodem", "luci-i18n-qmodem")):
                    continue
                path = Path(current) / name
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path)
                if name in directories:
                    directories.remove(name)
                print("Removed QModem: " + str(path.relative_to(tree)))


def build_identity(lock):
    started = datetime.now(timezone.utc)
    return dict(repository_owner=lock["repository_owner"],
                built_at=started.isoformat(timespec="seconds").replace("+00:00", "Z"),
                build_date=started.astimezone(timezone(timedelta(hours=8))).strftime("%y.%m.%d-%H.%M.%S"),
                profile_id=lock["profile_id"], ci_commit=os.environ.get("GITHUB_SHA", "local"),
                run_id=os.environ.get("GITHUB_RUN_ID", "local"), run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))


def customize_luci(tree, values, identity):
    root = tree / "feeds/luci"
    status_files = list((root / "modules/luci-mod-status").rglob("10_system.js"))
    flash_files = list((root / "modules/luci-mod-system").rglob("flash.js"))
    if not status_files or not flash_files:
        raise ValueError("LuCI status/upgrade modules are missing")
    for path in status_files:
        anchor = "(luciversion || '')"
        content = path.read_text()
        if content.count(anchor) != 1:
            raise ValueError("LuCI firmware version anchor changed upstream")
        stamp = " / " + identity["repository_owner"] + "-" + identity["build_date"]
        path.write_text(content.replace(anchor, anchor + " + " + json.dumps(stamp)))
    for path in flash_files:
        content, count = re.subn(r"(['\"])192\.168\.\d+\.\d+\1",
                                lambda m: m[1] + values["lan_ip"] + m[1], path.read_text())
        if not count:
            raise ValueError("LuCI default reconnect IP changed upstream")
        path.write_text(content)
    # Remove just the dependency token, preserving other packages and continuations.
    for path in (root / "collections").rglob("Makefile"):
        content = re.sub(r"\+(?:[^\s+]*:)?luci-app-attendedsysupgrade(?=[\s\\]|$)", "", path.read_text())
        path.write_text(content)


def cache_plan(args):
    lock, tree = read_json(args.lock), Path(args.source)
    # These Git tree IDs change whenever host/toolchain build rules change. Feed
    # and custom recipe commits also isolate hostpkg builds. Never cache target/.
    objects = {name: run("git", "rev-parse", "HEAD:" + name, cwd=tree)
               for name in ("tools", "toolchain", "include", "config", "scripts", "package", "Makefile", "rules.mk")}
    compatibility = dict(source={k: lock["source"][k] for k in ("repository", "branch", "feeds")},
                         config=lock["config"], packages=lock["packages"], objects=objects, ci_hash=lock["ci_hash"],
                         host=dict(image=os.environ.get("ImageVersion", "unknown"),
                                   compiler=run("gcc", "--version"), linker=run("ld", "--version")))
    digest = hashlib.sha256(json.dumps(compatibility, sort_keys=True).encode()).hexdigest()[:24]
    prefix = "toolchain-v1-Linux-ubuntu24-" + lock["profile_id"] + "-" + digest + "-"
    key = prefix + lock["source"]["commit"] + "-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    with open(args.github_output, "a") as output:
        output.write("prefix=" + prefix + "\nkey=" + key + "\n")


def refresh_toolchain_cache(tree):
    for root in (tree / "staging_dir").glob("*"):
        if root.is_symlink() or not root.is_dir() or not root.name.startswith(("host", "toolchain-")):
            continue
        for stamp in root.rglob("stamp"):
            if stamp.is_dir():
                for path in stamp.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        path.touch()
    (tree / "tmp").mkdir(exist_ok=True)
    (tree / "tmp/.build").write_text("1\n")


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
    identity = build_identity(lock)
    private = ROOT / "Scripts/PRIVATE.sh"
    if private.is_file():
        print("Applying trusted Scripts/PRIVATE.sh")
        env = {**os.environ, "GITHUB_WORKSPACE": str(ROOT), "WRT_CONFIG": lock["profile"]["legacy_name"],
               "WRT_TARGET": lock["profile"]["target"], "WRT_SUBTARGET": lock["profile"]["subtarget"],
               "WRT_IP": lock["defaults"]["lan_ip"], "WRT_MARK": identity["repository_owner"],
               "WRT_DATE": identity["build_date"], "WRT_BRANCH": lock["source"]["branch"],
               "WRT_INFO": lock["profile"]["source"]}
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        run("bash", str(private), cwd=tree, env=env)
    remove_qmodem(tree)
    vendor = lock["profile"].get("variant") == "mtk-vendor" or lock["profile"]["source"] == "chasey"
    apply_defaults(tree, lock["defaults"], vendor, lock["profile"].get("wireless_defaults"))
    apply_kernel_config(tree, lock["profile"])
    customize_luci(tree, lock["defaults"], identity)
    write_json(tree / ".build-identity.json", identity)
    write_json(tree / "files/usr/share/openwrt-build/build-info.json", identity)
    values = lock["config"]
    content = "".join("CONFIG_" + key + "=" + value + "\n" for key, value in values.items())
    (tree / ".config").write_text(content)
    run("make", "defconfig", cwd=tree)
    actual = (tree / ".config").read_text()
    validate_config(lock["profile_id"], lock["profile"], actual, values)
    actual_values = merge_config([actual])
    missing = [key for key, value in lock["requested_packages"].items()
               if value in ("y", "m") and actual_values.get(key) not in (value, "y")]
    if missing:
        raise ValueError("Private/temporary options not selected: " + ", ".join(missing))
    if getattr(args, "toolchain_cache", False):
        refresh_toolchain_cache(tree)
        run("make", "clean", cwd=tree)
    print("Configuration verified: " + lock["profile_id"])


def image_manifest(target, profile, profile_id=None):
    manifests = sorted(p for p in target.glob("*.manifest") if p.is_file())
    device_suffix = "-" + profile["image_match"] + ".manifest"
    platform = "-" + profile["target"] + "-" + profile["subtarget"]
    # Older CI exported PROFILE, which upstream used as a manifest suffix.
    # Match only this locked profile, then the normal target-wide manifest.
    suffixes = [device_suffix]
    if profile_id:
        suffixes.append(platform + "-" + profile_id + ".manifest")
    suffixes.append(platform + ".manifest")
    candidates = []
    for suffix in suffixes:
        candidates = [p for p in manifests if p.name.endswith(suffix)]
        if candidates:
            break
    if len(candidates) != 1:
        found = ", ".join(p.name for p in manifests) or "none"
        raise ValueError("Expected one matching package manifest; found: " + found)
    return candidates[0]


def validate_image_packages(target, profile, profile_id=None):
    required = set(profile.get("required_image_packages", []))
    manifest = image_manifest(target, profile, profile_id)
    installed = {line.split()[0] for line in manifest.read_text().splitlines() if line.strip()}
    missing = sorted(required - installed)
    if missing:
        raise ValueError("Firmware is missing packages: " + ", ".join(missing))
    forbidden = sorted(installed & set(profile.get("forbidden_image_packages", [])))
    if forbidden:
        raise ValueError("Firmware contains forbidden packages: " + ", ".join(forbidden))
    print("Package manifest verified: " + manifest.name)
    return manifest


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


def release_notes(lock, identity=None, config_name="Config-*.txt"):
    profile, values, source = lock["profile"], lock["defaults"], lock["source"]
    label = profile.get("display_name", lock["profile_id"])
    repository = source["repository"].removesuffix(".git")
    notes = "ImmortalWrt · " + label + "\n\n"
    if identity:
        notes += "构建：`" + identity["repository_owner"] + "` · 北京时间 `" + identity["build_date"] + "`（编译开始）。\n\n"
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
            notes += "不集成 luci-app-mesh（厂商无线驱动未上报 mesh point 能力）；daed / honk 默认关闭，HNAT / WARP 与透明代理需验证兼容性。"
        notes += "\n"
    if lock["profile_id"].startswith("7dr7299-"):
        notes += "\n> 仅适用于 TP-Link TL-7DR7299 v1。MT76 / MTK 的引导器及分区布局需分别核对，不保证直接互刷。"
        notes += "sysupgrade.itb 用于匹配布局的 OpenWrt 升级；recovery.itb 为恢复镜像，不是原厂首次刷入包；不发布引导器。"
        if profile["variant"] == "mtk-vendor":
            notes += "闭源版不集成 Mesh / DAWN；daed / honk 默认关闭，启用前验证 HNAT / WARP 兼容性。"
        notes += "\n"
    notes += "\n<details>\n<summary>源码与文件</summary>\n\n"
    notes += "源码：[" + source["branch"] + " · " + source["commit"][:7] + "](" + repository + "/commit/" + source["commit"] + ")。\n\n"
    notes += "校验：`sha256sums` · 完整配置：`" + config_name + "` · 包清单：`*.manifest` · 构建记录：`build-info.json`。\n\n"
    notes += "其余构建诊断保存在 Actions 的 logs 附件中。\n\n</details>\n\n"
    notes += "<!-- build-fingerprint:" + lock["fingerprint"] + " -->\n"
    notes += "<!-- build-ci:" + lock["ci_hash"] + " -->\n"
    notes += config_marker(lock["config"]) + "\n"
    return notes + source_marker(source) + "\n"


def collect(args):
    lock = read_json(args.lock)
    tree = Path(args.source)
    output = Path(args.output_dir)
    profile = lock["profile"]
    target = tree / "bin/targets" / profile["target"] / profile["subtarget"]
    validate_config(lock["profile_id"], profile, (tree / ".config").read_text(), lock["config"])
    identity = read_json(tree / ".build-identity.json")
    if identity["profile_id"] != lock["profile_id"]:
        raise ValueError("Build identity profile mismatch")
    manifest = validate_image_packages(target, profile, lock["profile_id"])
    if lock["profile_id"].startswith("7dr7299-"):
        for kind in ("sysupgrade", "recovery"):
            candidates = list(target.glob("*" + profile["image_match"] + "-" + kind + ".itb"))
            if len(candidates) != 1:
                raise ValueError("Expected one 7DR7299 " + kind + ".itb image")
    if lock["profile_id"] == "s20p-mtk":
        sysupgrades = list(target.glob("*" + profile["image_match"] + "*sysupgrade*.bin"))
        if len(sysupgrades) != 1:
            raise ValueError("Expected one S20P MTK sysupgrade image")
        validate_mtk_sysupgrade(sysupgrades[0])
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Collection output must be empty")
    stamp = lock["profile_id"] + "-" + identity["build_date"] + "-r" + identity["run_id"] + "-" + identity["run_attempt"]
    diagnostics = ROOT / "logs/metadata"
    diagnostics.mkdir(parents=True, exist_ok=True)
    images = []
    for path in sorted(target.iterdir()):
        if not path.is_file():
            continue
        is_image = profile["image_match"] in path.name and path.name.endswith(IMAGE_EXTENSIONS) and (
            any(s in path.name for s in ("sysupgrade", "factory", "initramfs")) or
            (lock["profile_id"].startswith("7dr7299-") and path.name.endswith("-recovery.itb")) or
            (profile["target"] == "x86" and re.search(r"\.(img(?:\.gz)?|vmdk|vdi|qcow2)$", path.name)))
        if is_image:
            name = dated_filename(path.name, stamp)
            shutil.copy2(path, output / name)
            images.append(name)
        elif path.name.endswith((".buildinfo", ".json")):
            shutil.copy2(path, diagnostics / path.name)
    if not images:
        raise ValueError("No matching firmware images produced")
    config_name = "Config-" + stamp + ".txt"
    shutil.copy2(tree / ".config", output / config_name)
    shutil.copy2(manifest, output / (stamp + ".manifest"))
    (diagnostics / "config.seed").write_text("".join("CONFIG_" + key + "=" + value + "\n" for key, value in lock["config"].items()))
    (diagnostics / "config.diff").write_text(run("./scripts/diffconfig.sh", cwd=tree) + "\n")
    assets = sorted(p.name for p in output.iterdir()) + ["build-info.json", "sha256sums"]
    info = {**{k: v for k, v in lock.items() if k not in ("config", "requested_packages")}, **identity,
            "images": images, "release_assets": assets,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    write_json(output / "build-info.json", info)
    (output / "release-notes.md").write_text(release_notes(lock, identity, config_name))
    checksums = []
    for name in sorted(assets):
        path = output / name
        if name != "sha256sums":
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            checksums.append(digest.hexdigest() + "  " + path.name + "\n")
    (output / "sha256sums").write_text("".join(checksums))


IMAGE_EXTENSIONS = (".img.gz", ".tar.gz", ".bin", ".itb", ".img", ".vmdk", ".vdi", ".qcow2",
                    ".tar", ".ubi", ".trx", ".chk", ".elf", ".uImage", ".fit")


def dated_filename(name, stamp):
    # Keep multi-part image extensions intact, e.g. .img.gz and .tar.gz.
    suffix = "".join(Path(name).suffixes)
    # Some source names contain dots in version numbers; only use known endings.
    ending = next((s for s in IMAGE_EXTENSIONS if name.endswith(s)), suffix)
    return name[:-len(ending)] + "-" + stamp + ending if ending else name + "-" + stamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--profile", default="all")
    p.add_argument("--commit")
    p.add_argument("--packages", default="")
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
        else:
            p.add_argument("--toolchain-cache", action="store_true")
    p = sub.add_parser("cache-plan")
    p.add_argument("--source", required=True)
    p.add_argument("--lock", required=True)
    p.add_argument("--github-output", required=True)
    args = parser.parse_args()
    {"plan": plan, "prepare": prepare, "collect": collect, "cache-plan": cache_plan}[args.command](args)


if __name__ == "__main__":
    main()
