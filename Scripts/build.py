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
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")
NETWORK_WORKERS = 4
CI_INPUTS = ("Config", "Scripts", ".github", ".gitattributes")
FIRMWARE_INPUTS = (
    ".gitattributes", ".github/workflows/build.yml",
    "Scripts/build.py", "Scripts/compile.sh", "Scripts/install-dependencies.sh", "Scripts/uci-defaults",
    "Scripts/Patches/ustream-ssl-partial-write.patch", "Scripts/Patches/ustream-ssl-partial-write-modern.patch",
)
GENERATED_DIRS = {".git", ".state", "out", "logs", "__pycache__", "openwrt", "tests"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def update_file_hash(digest, path):
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)


def file_hash(path):
    digest = hashlib.sha256()
    update_file_hash(digest, path)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_lock(path):
    lock = read_json(path)
    if not isinstance(lock, dict) or lock.get("fingerprint") != fingerprint(
            {key: value for key, value in lock.items() if key != "fingerprint"}):
        raise ValueError("Build lock contents do not match their fingerprint: " + str(path))
    return lock


def read_toml(path):
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(*args, cwd=None, env=None, timeout=600):
    try:
        return subprocess.check_output(args, cwd=cwd, env=env, encoding="utf-8", timeout=timeout).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # Captured stdout often contains the useful feeds/make diagnostic.
        if error.output:
            output = error.output
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            print(output, flush=True)
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
        except urllib.error.HTTPError as error:
            retryable = error.code in (408, 429, 500, 502, 503, 504) or (
                error.code == 403 and (error.headers or {}).get("X-RateLimit-Remaining") == "0")
            if not retryable or attempt == 2:
                raise
            error.close()
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
    lines = run("git", "ls-remote", repo, ref, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=120).splitlines()
    if len(lines) != 1 or not SHA.fullmatch(lines[0].split()[0]):
        raise ValueError("Cannot resolve source: " + repo + " " + ref)
    return lines[0].split()[0]


def resolve_refs(refs, resolved=None):
    """Resolve each unique Git reference once, with bounded network concurrency."""
    resolved = dict(resolved or {})
    pending = list(dict.fromkeys(ref for ref in refs if ref not in resolved))
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=NETWORK_WORKERS) as pool:
            resolved.update(zip(pending, pool.map(lambda ref: resolve(*ref), pending)))
    return resolved


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


PROJECT_CONFIG = {
    "TARGET_ALL_PROFILES": "n",
    "TARGET_MULTI_PROFILE": "y",
    "TARGET_PER_DEVICE_ROOTFS": "n",
    "TARGET_ROOTFS_INITRAMFS": "n",
    # OpenWrt exposes CCACHE only when DEVEL is enabled.
    "DEVEL": "y",
    "CCACHE": "y",
    "CCACHE_DIR": '\"\"',
}


def serialize_config(values):
    return "".join("CONFIG_" + key + "=" + value + "\n" for key, value in values.items())


def profile_platform(profile, values=None):
    """Read the single target/subtarget/device selection from the profile Config."""
    if values is None:
        values = merge_config([(ROOT / profile["config"]).read_text()])
    devices = []
    for key, value in values.items():
        match = re.fullmatch(r"TARGET_DEVICE_(.+)_DEVICE_(.+)", key)
        if match and value == "y":
            devices.append((match.group(1), match.group(2)))
    if len(devices) != 1:
        raise ValueError(profile["config"] + ": expected exactly one selected device")
    platform, device = devices[0]
    excluded = (
        "TARGET_ALL_PROFILES", "TARGET_MULTI_PROFILE", "TARGET_PER_DEVICE_ROOTFS",
        "TARGET_ROOTFS_", "TARGET_IMAGES_", "TARGET_KERNEL_", "TARGET_INITRAMFS_",
        "TARGET_DEVICE_",
    )
    targets = [key.removeprefix("TARGET_") for key, value in values.items()
               if value == "y" and key.startswith("TARGET_") and not key.startswith(excluded)]
    candidates = [target for target in targets if platform.startswith(target + "_")]
    if not candidates:
        raise ValueError(profile["config"] + ": target and subtarget are not both selected")
    target = min(candidates, key=len)
    subtarget = platform[len(target) + 1:]
    target_key = "TARGET_" + target
    subtarget_key = "TARGET_" + target + "_" + subtarget
    device_key = "TARGET_DEVICE_" + platform + "_DEVICE_" + device
    if not subtarget or values.get(target_key) != "y" or values.get(subtarget_key) != "y":
        raise ValueError(profile["config"] + ": target and subtarget are not both selected")
    return dict(target=target, subtarget=subtarget, device=device, device_key=device_key)


def config_for(profile, packages="", private_content=None):
    selections = [serialize_config(PROJECT_CONFIG), (ROOT / profile["config"]).read_text()]
    if private_content is None:
        private = ROOT / "Config/PRIVATE.txt"
        private_content = private.read_text() if private.is_file() else ""
    selections.append(private_content)
    selections.append(package_overrides(packages))
    values = merge_config(selections)
    platform = profile_platform(profile)
    if profile_platform(profile, values) != platform:
        raise ValueError(profile["config"] + ": overrides must retain the profile target, subtarget and device")
    return values


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


def validate_config(profile_id, profile, content, selections=None):
    values = merge_config([content])
    selections = config_for(profile) if selections is None else selections
    platform = profile_platform(profile)
    errors = []
    requirements = {**selections, "TARGET_" + platform["target"]: "y",
                    "TARGET_" + platform["target"] + "_" + platform["subtarget"]: "y",
                    platform["device_key"]: "y"}
    for key, expected in requirements.items():
        actual = values.get(key)
        if expected in ("y", "m"):
            if actual not in (expected, "y"):
                errors.append("Missing CONFIG_" + key + "=" + expected + "; got " + str(actual or "<missing>"))
        elif expected == "n":
            if actual in ("y", "m"):
                errors.append("Expected CONFIG_" + key + " to remain disabled; got " + actual)
        elif actual != expected:
            errors.append("Expected CONFIG_" + key + "=" + expected + "; got " + str(actual or "<missing>"))
    if any(key.startswith("TARGET_DEVICE_") and value == "y" and key != platform["device_key"]
           for key, value in values.items()):
        errors.append("More than one device selected")
    if values.get("TARGET_ALL_PROFILES") == "y":
        errors.append("All-device builds are not allowed")
    if errors:
        raise ValueError(profile_id + " configuration mismatch:\n" + "\n".join(errors))


def defaults(profile_id=None, specs=None, values=None):
    values = dict(read_toml(ROOT / "Config/firmware-defaults.toml") if values is None else values)
    if profile_id is not None:
        specs = read_toml(ROOT / "Config/profiles.toml") if specs is None else specs
        profiles = specs["profiles"]
        profile = profiles[profile_id]
        source = specs["sources"][profile["source"]]
        values["hostname"] = profile["hostname"]
        if source.get("network_tuning") and profile_platform(profile)["target"] != "x86":
            values["network_tuning"] = True
    return values


def input_hash(inputs):
    files = []
    for relative in inputs:
        root = ROOT / relative
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            for current, directories, names in os.walk(root, followlinks=False):
                directories[:] = [name for name in directories if name not in GENERATED_DIRS]
                files.extend(Path(current) / name for name in names
                             if name not in GENERATED_DIRS and name != ".DS_Store" and not name.endswith(".pyc")
                             and (Path(current) / name).is_file())
    digest = hashlib.sha256()
    for path in sorted(files):
        rel = path.relative_to(ROOT)
        digest.update(str(rel).encode() + b"\0")
        update_file_hash(digest, path)
        digest.update(b"\0")
    return digest.hexdigest()


def ci_hash():
    return input_hash(CI_INPUTS)


def firmware_fingerprint(lock, shared_inputs):
    paths = list(lock["profile"].get("patches", []))
    if lock["defaults"].get("network_tuning"):
        paths.append("Scripts/Files/network-tuning.conf")
    if any(package["mode"] == "honk" for package in lock["packages"].values()):
        paths.append("Scripts/Honk.sh")
    if "xray" in lock:
        paths.append("Scripts/Makefiles/xray-core-prebuilt.mk")

    inputs = {key: value for key, value in lock.items() if key not in ("ci_hash", "requested_packages")}
    inputs["packages"] = [(name, {key: value for key, value in package.items() if key != "profiles"})
                          for name, package in lock["packages"].items()]
    return fingerprint(dict(inputs=inputs, shared=shared_inputs, files=input_hash(paths)))


def firmware_marker(digest):
    return "<!-- firmware-fingerprint:" + digest + " -->"


def latest_xray_release():
    releases = json.loads(request("https://api.github.com/repos/XTLS/Xray-core/releases?per_page=100"))
    release = max((r for r in releases if not r["draft"] and r.get("published_at")),
                  key=lambda r: (r["published_at"], r["id"]), default=None)
    if release is None:
        raise ValueError("No published Xray release found")
    return release


def xray_lock(profile_id, release=None):
    asset_name = "Xray-linux-64.zip" if profile_id == "x86-64" else "Xray-linux-arm64-v8a.zip"
    release = latest_xray_release() if release is None else release
    asset = next((a for a in release["assets"] if a["name"] == asset_name and a["state"] == "uploaded"), None)
    if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
        raise ValueError("Latest Xray asset is absent or has no SHA256; refusing an older/unchecked binary")
    tag = release["tag_name"]
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", tag):
        raise ValueError("Invalid Xray tag")
    return dict(tag=tag, version=tag.removeprefix("v"), asset=asset_name, asset_id=asset["id"], sha256=asset["digest"][7:])


def source_marker(source):
    identity = {key: source[key] for key in ("repository", "branch", "commit")}
    return "<!-- build-source:" + fingerprint(identity) + " -->"


def config_marker(values):
    return "<!-- build-config:" + fingerprint(values) + " -->"


def ccache_group(name, profile, platform=None):
    # The same compatibility group also identifies the shared toolchain cache.
    platform = profile_platform(profile) if platform is None else platform
    group = "-".join((profile["source"], platform["target"], platform["subtarget"])).replace("_", "-")
    if not isinstance(group, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", group):
        raise ValueError("Invalid ccache group: " + name)
    return group


TOOLCHAIN_CONFIG_PREFIXES = (
    "TARGET_", "GCC_", "BINUTILS_", "LIBC_", "MUSL_", "UCLIBC_", "GLIBC_",
    "USE_", "TOOLCHAIN_", "ARCH_", "CPU_", "LINUX_",
    "KERNEL_GIT_", "TESTING_KERNEL", "EXTERNAL_KERNEL_TREE", "EXTERNAL_TOOLCHAIN",
    "SOFT_FLOAT", "HAS_FPU",
)
TOOLCHAIN_CONFIG_EXCLUDES = (
    "TARGET_DEVICE", "TARGET_PROFILE", "TARGET_ALL_PROFILES",
    "TARGET_MULTI_PROFILE", "TARGET_PER_DEVICE", "TARGET_ROOTFS",
    "TARGET_IMAGES", "TARGET_INITRAMFS", "TARGET_KERNEL",
)
# Additional selectors used by tools/Makefile and the compiler recipes.
TOOLCHAIN_CONFIG_OPTIONS = {
    "BRCMSMAC_USE_FW_FROM_WL", "BUILD_ALL_HOST_TOOLS", "BUILD_BINDGEN", "BUILDBOT",
    "CCACHE", "DWARVES", "EFI_IMAGES", "GDB", "NASM", "SDK", "PACKAGE_kmod-b43",
    "EXTRA_GCC_CONFIG_OPTIONS", "EXTRA_OPTIMIZATION", "EXTRA_TARGET_ARCH", "EXTRA_TARGET_ARCH_NAME",
    "INSTALL_GCCGO", "INSTALL_GFORTRAN", "MIPS64_ABI", "mips64", "mips64el", "powerpc64",
    "RUST_SCCACHE", "RUST_TOOLCHAIN_HOST_PATH",
    "TARGET_INITRAMFS_COMPRESSION_BZIP2", "TARGET_INITRAMFS_COMPRESSION_LZ4", "TARGET_INITRAMFS_COMPRESSION_LZO",
}


def toolchain_config(values):
    """Keep only settings that can affect host tools or the target toolchain."""
    return {key: value for key, value in values.items()
            if key in TOOLCHAIN_CONFIG_OPTIONS or (key.startswith(TOOLCHAIN_CONFIG_PREFIXES)
                                                   and not key.startswith(TOOLCHAIN_CONFIG_EXCLUDES)
                                                   and "_DEVICE_" not in key)}


def assign_cache_writers(matrix, locks):
    platforms, writers = {}, {}
    for item in matrix:
        name = item["profile"]
        profile, selected = locks[name]["profile"], locks[name]["platform"]
        group = ccache_group(name, profile, selected)
        platform = (profile["source"], selected["target"], selected["subtarget"])
        if platforms.setdefault(group, platform) != platform:
            raise ValueError("A ccache group must use the same source and platform: " + group)
        item["ccache_group"] = group
        item["toolchain_group"] = group
        if group not in writers or name < writers[group]:
            writers[group] = name
    for item in matrix:
        item["ccache_write"] = item["profile"] == writers[item["ccache_group"]]


def compression_profile(profile_id, profile, compression="profile"):
    if compression not in ("profile", "lzma", "gzip"):
        raise ValueError("Invalid kernel compression")
    if profile_id != "jdcloud-re-cs-06":
        if compression != "profile":
            raise ValueError("A kernel compression override requires jdcloud-re-cs-06")
        return dict(profile)
    selected = "lzma" if compression == "profile" else compression
    if selected not in ("lzma", "gzip"):
        raise ValueError("Invalid RE-CS-06 kernel compression")
    lzma_patch = "Scripts/Patches/jdcloud-re-cs-06-lzma-kernel.patch"
    patches = [path for path in profile.get("patches", []) if path != lzma_patch]
    if selected == "lzma":
        patches.insert(0, lzma_patch)
    return {**profile, "kernel_compression": selected, "patches": patches}


def plan(args):
    code_hash = ci_hash()
    settings = read_toml(ROOT / "Config/build-settings.toml")
    if type(settings["scheduled_builds"]) is not bool:
        raise ValueError("scheduled_builds must be a boolean")
    for key in ("max_parallel", "artifact_retention_days"):
        if type(settings[key]) is not int or settings[key] < 1:
            raise ValueError(key + " must be a positive integer")
    matrix = []
    locks = {}
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    specs = read_toml(ROOT / "Config/profiles.toml")
    if args.profile != "all" and args.profile not in specs["profiles"]:
        raise ValueError("Unknown profile")
    if args.commit and args.profile == "all":
        raise ValueError("A commit override requires a single profile")
    if args.commit and not SHA.fullmatch(args.commit):
        raise ValueError("A pinned commit must be a full 40-character SHA")
    compression = getattr(args, "kernel_compression", "profile")
    if compression != "profile" and args.profile != "jdcloud-re-cs-06":
        raise ValueError("A kernel compression override requires jdcloud-re-cs-06")
    overrides = package_overrides(getattr(args, "packages", ""))
    owner = os.environ.get("GITHUB_REPOSITORY", "local/local").split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ValueError("Invalid repository owner")
    if args.event != "schedule" or settings["scheduled_builds"]:
        profiles = {name: compression_profile(name, profile, compression)
                    for name, profile in specs["profiles"].items() if args.profile in ("all", name)}
        # A private hook can depend on any project file.
        shared_inputs = code_hash if (ROOT / "Scripts/PRIVATE.sh").is_file() else input_hash(FIRMWARE_INPUTS)
        base_defaults = defaults()
        private = ROOT / "Config/PRIVATE.txt"
        private_content = private.read_text() if private.is_file() else ""
        configurations = {name: config_for(profile, overrides, private_content)
                          for name, profile in profiles.items()}
        known = []
        if args.event == "schedule" or not args.force:
            repository = os.environ.get("GITHUB_REPOSITORY")
            if not repository:
                raise ValueError("Release comparison requires GITHUB_REPOSITORY; use --force for a local plan")
            pages = json.loads(run("gh", "api", "repos/" + repository + "/releases?per_page=100", "--paginate", "--slurp"))
            known = [r for page in pages for r in page if not r["draft"]]
            known.sort(key=lambda r: (r.get("published_at") or "", r["id"]), reverse=True)
        packages = read_toml(ROOT / "Config/packages.toml")
        selected_packages = {
            name: {key: package for key, package in packages.items()
                   if "all" in package["profiles"] or name in package["profiles"]}
            for name in profiles}
        source_refs = {profile["source"]: (specs["sources"][profile["source"]]["repository"],
                                          specs["sources"][profile["source"]]["branch"], args.commit)
                       for profile in profiles.values()}
        package_refs = [(package["repository"], package.get("branch"), package.get("commit"))
                        for selected in selected_packages.values() for package in selected.values()]
        resolved = resolve_refs([*source_refs.values(), *package_refs])

        def load_source(source_id):
            source = specs["sources"][source_id]
            commit = resolved[source_refs[source_id]]
            slug = source["repository"].removeprefix("https://github.com/").removesuffix(".git")
            content = request("https://raw.githubusercontent.com/" + slug + "/" + commit + "/feeds.conf.default").decode()
            return {**source, "commit": commit, "feeds": parse_feeds(content)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=NETWORK_WORKERS) as pool:
            sources = dict(zip(source_refs, pool.map(load_source, source_refs)))
        feed_refs = [(feed["repository"], feed["branch"], feed["commit"])
                     for source in sources.values() for feed in source["feeds"]]
        resolved = resolve_refs(feed_refs, resolved)
        for source in sources.values():
            source["feeds"] = [{**feed, "commit": resolved[(feed["repository"], feed["branch"], feed["commit"])]}
                               for feed in source["feeds"]]
        xray_release, xray_assets = None, {}
        for profile_id, profile in profiles.items():
            platform = profile_platform(profile)
            source_id = profile["source"]
            selected = {}
            for name, package in selected_packages[profile_id].items():
                key = (package["repository"], package.get("branch"), package.get("commit"))
                selected[name] = {**package, "commit": resolved[key]}
            lock = dict(profile_id=profile_id, profile=profile, platform=platform,
                        source=sources[source_id], packages=selected,
                        ci_hash=code_hash, defaults=defaults(profile_id, specs, base_defaults),
                        config=configurations[profile_id],
                        repository_owner=owner)
            lock["requested_packages"] = merge_config([private_content, overrides])
            if lock["config"].get("PACKAGE_xray-core") == "y":
                if xray_release is None:
                    xray_release = latest_xray_release()
                arch = "x86-64" if profile_id == "x86-64" else "arm64"
                if arch not in xray_assets:
                    xray_assets[arch] = xray_lock(profile_id, xray_release)
                lock["xray"] = xray_assets[arch]
            lock["firmware_fingerprint"] = firmware_fingerprint(lock, shared_inputs)
            lock["fingerprint"] = fingerprint(lock)
            marker = firmware_marker(lock["firmware_fingerprint"])
            if args.event == "schedule":
                # The fingerprint includes the package commits resolved from upstream branches.
                latest = next((r for r in known if r["tag_name"].startswith(profile_id + "-")), None)
                unchanged = bool(latest and marker in (latest.get("body") or ""))
            else:
                unchanged = any(r["tag_name"].startswith(profile_id + "-")
                                and marker in (r.get("body") or "") for r in known)
            if unchanged:
                print("Unchanged: " + profile_id)
                continue
            locks[profile_id] = lock
            variant = profile.get("kernel_compression", "default")
            group = "firmware-" + profile_id + ("-" + variant if variant != "default" else "")
            matrix.append(dict(profile=profile_id, source_commit=lock["source"]["commit"], fingerprint=lock["fingerprint"],
                               kernel_compression=variant, concurrency_group=group))
    assign_cache_writers(matrix, locks)
    if ci_hash() != code_hash:
        raise ValueError("CI files changed while planning; retry with a stable checkout")
    result = dict(matrix=dict(include=matrix), has_builds=bool(matrix), max_parallel=settings["max_parallel"],
                  retention_days=settings["artifact_retention_days"])
    for profile_id, lock in locks.items():
        write_json(output / (profile_id + ".json"), lock)
    write_json(output / "matrix.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if args.github_output:
        with open(args.github_output, "a") as stream:
            for key, value in result.items():
                stream.write(key + "=" + json.dumps(value, separators=(",", ":")) + "\n")


def checkout(repo, commit, dest):
    if os.path.lexists(dest):
        raise FileExistsError("Checkout destination already exists: " + str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="." + dest.name + "-", dir=dest.parent) as temporary:
        tree = Path(temporary) / "source"
        git = ("git", "-C", str(tree))
        run("git", "init", "-q", str(tree))
        run(*git, "remote", "add", "origin", repo)
        run(*git, "fetch", "--depth=1", "--no-tags", "origin", commit, env=env)
        run(*git, "checkout", "-q", "--detach", "FETCH_HEAD")
        if run(*git, "rev-parse", "HEAD") != commit:
            raise ValueError("Source checkout differs from build plan")
        tree.rename(dest)


def checkout_locked(args):
    lock = read_lock(args.lock)
    if lock["source"]["commit"] != args.commit:
        raise ValueError("Source commit does not match the build plan")
    if lock["fingerprint"] != args.fingerprint:
        raise ValueError("Build fingerprint changed after planning")
    checkout(lock["source"]["repository"], lock["source"]["commit"], Path(args.source))


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
    env = {**os.environ, "GITHUB_WORKSPACE": str(ROOT), "WRT_PROFILE": lock["profile_id"]}
    special_packages = {
        "honk": ("Honk.sh", "HONK_COMMIT"),
    }
    for name, package in lock["packages"].items():
        mode = package["mode"]
        if mode in special_packages:
            script, commit_env = special_packages[mode]
            env[commit_env] = package["commit"]
            run("bash", str(ROOT / "Scripts" / script), cwd=tree, env=env)
            continue
        with tempfile.TemporaryDirectory(prefix="owrt-package-") as temporary:
            clone = Path(temporary) / "source"
            checkout(package["repository"], package["commit"], clone)
            if mode == "golang-toolchain":
                install_daed_go_toolchain(tree, clone, package["version"])
                continue
            dirs = package.get("directories") or (["."] if (clone / "Makefile").exists() else
                                                      [p.name for p in clone.iterdir() if p.is_dir() and (p / "Makefile").exists()])
            if mode == "resources":
                for directory in dirs:
                    source = clone / directory
                    if not source.is_dir():
                        raise ValueError(name + ": missing resource directory " + directory)
                    destination = tree / "package" / directory
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source, destination, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(".git", ".github"))
                continue
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
    if "golang-daed" in lock["packages"]:
        use_daed_go_toolchain(tree, lock["packages"]["golang-daed"]["version"])
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
    # Maintain the old Rust build fix without unrelated theme/download hacks.
    for path in (tree / "feeds/packages").glob("*/rust/Makefile"):
        path.write_text(path.read_text().replace("ci-llvm=true", "ci-llvm=false"))


def install_daed_go_toolchain(tree, clone, version):
    if not re.fullmatch(r"1\.[0-9]+", version):
        raise ValueError("Invalid dae/daed Go version")
    source = clone / "lang/golang"
    for relative in ("golang-package.mk", "golang-values.mk", "golang-compiler.mk", "golang-version.mk",
                     "golang-bootstrap/Makefile", "golang" + version + "/Makefile"):
        if not (source / relative).is_file():
            raise ValueError("Versioned Go toolchain changed upstream: " + relative)
    # Versioned host binaries coexist with the feed's go-cross compiler. Do not
    # import the default 'golang' alias or replace the upstream feed/helpers.
    shutil.copytree(source, tree / "package/golang-daed", ignore=shutil.ignore_patterns("golang"))


def use_daed_go_toolchain(tree, version):
    for name in ("dae", "daed"):
        path = tree / "package" / name / "Makefile"
        content, dependencies = re.subn(r"(?m)^(PKG_BUILD_DEPENDS[ \t]*:?=[ \t]*)golang/host(?=[ \t]|$)",
                                        lambda m: m[1] + "golang" + version + "/host", path.read_text())
        content, includes = re.subn(r"(?m)^include \$\(TOPDIR\)/feeds/packages/lang/golang/golang-package\.mk[ \t]*$",
                                   "include $(TOPDIR)/package/golang-daed/golang-package.mk", content)
        if dependencies != 1 or includes != 1:
            raise ValueError(name + ": Go build declarations changed upstream")
        path.write_text(content)


def use_packaged_adguardhome(tree):
    # Use the packaged core without changing its version, Go or service files.
    path = tree / "package/custom/luci-app-adguardhome/root/etc/config/AdGuardHome"
    content, count = re.subn(r"(?m)^([ \t]*option[ \t]+binpath[ \t]+)'[^'\n]*'[ \t]*$",
                            lambda m: m[1] + "'/usr/bin/AdGuardHome'", path.read_text())
    if count != 1:
        raise ValueError("AdGuard Home binary path defaults changed upstream")
    path.write_text(content)


def apply_source_patches(tree, patches):
    for name in patches:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("Scripts", "Patches"):
            raise ValueError("Invalid source patch path")
        path = ROOT / relative
        if not path.is_file():
            raise ValueError("Source patch is missing: " + name)
        print("Applying source patch: " + name, flush=True)
        run("patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(path), cwd=tree)


def makefile_value(content, name, filename="ustream-ssl Makefile"):
    match = re.search(r"(?m)^" + re.escape(name) + r"\s*(?::|\?)?=\s*(.*?)\s*$", content)
    if not match or not match.group(1):
        raise ValueError(filename + " missing " + name)
    return match.group(1).strip()


def c_function_body(source, name):
    match = re.search(r"(?ms)^static int " + re.escape(name) + r"\([^\n]*\)\s*\n\{(.*?)^\}", source)
    if not match:
        raise ValueError("ustream-ssl source missing " + name + "()")
    return match.group(1)


def ustream_ssl_write_is_fixed(source):
    body = c_function_body(source, "ustream_ssl_write")
    loop = re.search(r"\bwhile\s*\(\s*(written|done)\s*<\s*len\s*\)", body)
    return bool(loop and re.search(r"\breturn\s+" + loop.group(1) + r"\s*;", body))


def ustream_ssl_write_patch(source):
    """Select the partial-write patch matching the upstream write implementation."""
    if ustream_ssl_write_is_fixed(source):
        return None
    body = c_function_body(source, "ustream_ssl_write")
    if "pending_write" in body:
        name = "ustream-ssl-partial-write.patch"
    elif re.search(r"\breturn\s+__ustream_ssl_write\(us,\s*buf,\s*len\);", body):
        name = "ustream-ssl-partial-write-modern.patch"
    else:
        raise ValueError("ustream-ssl write implementation changed upstream; update the partial-write patches")
    patch = ROOT / "Scripts/Patches" / name
    if not patch.is_file():
        raise ValueError("ustream-ssl partial-write patch is missing: " + name)
    return patch


def fetch_ustream_ssl_source(tree, makefile_text):
    download_mk = tree / "include/download.mk"
    if not download_mk.is_file():
        raise ValueError("ustream-ssl source metadata missing: include/download.mk")
    project_git = makefile_value(download_mk.read_text(), "PROJECT_GIT", "include/download.mk").rstrip("/")
    source_url = makefile_value(makefile_text, "PKG_SOURCE_URL")
    source_url = source_url.replace("$(PROJECT_GIT)", project_git).replace("${PROJECT_GIT}", project_git)
    if "$(" in source_url or "${" in source_url:
        raise ValueError("Cannot resolve ustream-ssl source URL: " + source_url)
    if not source_url.startswith("https://"):
        raise ValueError("ustream-ssl source URL must use HTTPS: " + source_url)
    source_proto = makefile_value(makefile_text, "PKG_SOURCE_PROTO")
    if source_proto != "git":
        raise ValueError("Unsupported ustream-ssl source protocol: " + source_proto)
    version = makefile_value(makefile_text, "PKG_SOURCE_VERSION")
    if not SHA.fullmatch(version):
        raise ValueError("ustream-ssl source version must be a full commit SHA: " + version)
    with tempfile.TemporaryDirectory(prefix="ustream-ssl-source-") as directory:
        repo = Path(directory)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        run("git", "-C", str(repo), "init", "--quiet", env=env)
        run("git", "-C", str(repo), "remote", "add", "origin", source_url, env=env)
        run("git", "-C", str(repo), "fetch", "--quiet", "--depth=1", "--no-tags", "origin", version, env=env)
        files = run("git", "-C", str(repo), "ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines()
        if "ustream-ssl.c" not in files:
            raise ValueError("ustream-ssl source layout changed: ustream-ssl.c is missing")
        source = run("git", "-C", str(repo), "show", "FETCH_HEAD:ustream-ssl.c") + "\n"
    return source, version


def ustream_ssl_package_patches(patches, target):
    """Return existing package patches that modify ustream-ssl.c."""
    selected = []
    for patch in sorted(patches.glob("*.patch")):
        if patch.name == target.name or not patch.is_file():
            continue
        content = patch.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?m)^(?:diff --git a/ustream-ssl\.c b/ustream-ssl\.c|"
                     r"(?:---|\+\+\+) [ab]/ustream-ssl\.c(?:\s.*)?)$", content):
            selected.append(patch)
    return selected


def stage_ustream_ssl_write_patch(source, version, patches, target):
    """Replay package patches, then select and validate the project write fix."""
    with tempfile.TemporaryDirectory(prefix="ustream-ssl-patch-") as directory:
        source_dir = Path(directory)
        source_path = source_dir / "ustream-ssl.c"
        source_path.write_text(source, encoding="utf-8")
        # Keep patch paths relative to this temporary tree even if the system
        # temporary directory happens to be nested inside another Git repo.
        run("git", "init", "--quiet", cwd=source_dir)
        # OpenWrt applies package patches before our lexicographically last 900 patch.
        for package_patch in ustream_ssl_package_patches(patches, target):
            print("Applying existing ustream-ssl package patch: " + package_patch.name, flush=True)
            try:
                run("git", "apply", "--check", "--whitespace=nowarn", "--include=ustream-ssl.c",
                    "-p1", str(package_patch), cwd=source_dir)
                run("git", "apply", "--whitespace=nowarn", "--include=ustream-ssl.c",
                    "-p1", str(package_patch), cwd=source_dir)
            except subprocess.CalledProcessError:
                raise ValueError("ustream-ssl " + version + " no longer accepts existing package patch " +
                                 package_patch.name) from None
        patch = ustream_ssl_write_patch(source_path.read_text(encoding="utf-8"))
        if patch is None:
            target.unlink(missing_ok=True)
            print("Skipping ustream-ssl write fix: upstream " + version +
                  " and its package patches already flush partial writes", flush=True)
            return
        try:
            run("git", "apply", "--check", "--whitespace=nowarn", "-p1", str(patch), cwd=source_dir)
            run("git", "apply", "--whitespace=nowarn", "-p1", str(patch), cwd=source_dir)
        except subprocess.CalledProcessError:
            raise ValueError("ustream-ssl " + version +
                             " changed and " + patch.name + " no longer applies; update that patch") from None
        if not ustream_ssl_write_is_fixed(source_path.read_text(encoding="utf-8")):
            raise ValueError("ustream-ssl " + version + " and " + patch.name +
                             " do not produce the expected partial-write fix")
    patches.mkdir(exist_ok=True)
    shutil.copyfile(patch, target)
    print("Staging ustream-ssl package patch: " + target.name + " for " + version, flush=True)


def apply_ustream_ssl_write_patch(tree):
    """Validate and stage the partial-write fix across ustream-ssl updates."""
    package = tree / "package/libs/ustream-ssl"
    makefile = package / "Makefile"
    if not makefile.is_file():
        raise ValueError("ustream-ssl package recipe is missing")
    source, version = fetch_ustream_ssl_source(tree, makefile.read_text())
    patches = package / "patches"
    target = patches / "900-flush-partial-ssl-writes.patch"
    stage_ustream_ssl_write_patch(source, version, patches, target)


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


def shell_quote(value):
    if not isinstance(value, str) or any(character in value for character in "\r\n\0"):
        raise ValueError("Firmware defaults must be single-line strings")
    return shlex.quote(value)


def uci_batch_quote(value):
    # The shell expands here-documents even inside UCI's quote characters.
    # Escape for that outer layer after quoting the value for UCI's parser.
    return shell_quote(value).replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`")


UCODE_TOKEN = re.compile(
    r"""(?P<comment>//[^\n]*|/\*.*?\*/)|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
    r"""|\x60(?:\\.|[^\x60\\])*\x60|[\w$]+|\S""",
    re.S,
)


def ucode_properties(content, start):
    """Yield top-level property names and value spans, leaving comments at the edges."""
    tokens, nesting = [], []
    pairs = {"(": ")", "[": "]", "{": "}"}
    for token in UCODE_TOKEN.finditer(content, start):
        if token.lastgroup == "comment":
            continue
        value = token[0]
        if not nesting and value in (",", "}"):
            if len(tokens) >= 3 and tokens[1][0] == ":":
                yield tokens[0][0].strip("'\""), tokens[2].start(), tokens[-1].end()
            if value == "}":
                return
            tokens.clear()
            continue
        if value in pairs:
            nesting.append(pairs[value])
        elif value in pairs.values() and (not nesting or nesting.pop() != value):
            break
        tokens.append(token)
    raise ValueError("mac80211 wifi-iface section changed upstream")


def rewrite_mac80211_defaults(content, values):
    """Patch OpenWrt's generated Wi-Fi defaults while retaining fail-closed checks."""
    defaults = dict(ssid=values["wifi_ssid"], key=values["wifi_password"],
                    encryption=values["wifi_encryption"], disabled=str(int(values["wifi_disabled"])))
    blocks = list(re.finditer(r"""set_section\(\s*iface\s*,\s*(['"])wifi-iface\1\s*,\s*\{""", content))
    if blocks:
        if len(blocks) != 1:
            raise ValueError("mac80211 wifi-iface section changed upstream")
        fields = {}
        for key, start, end in ucode_properties(content, blocks[0].end()):
            if key not in defaults:
                continue
            if key in fields:
                raise ValueError("mac80211 " + key + " defaults changed upstream")
            fields[key] = (start, end)
        if missing := defaults.keys() - fields.keys():
            raise ValueError("mac80211 " + ", ".join(sorted(missing)) + " defaults changed upstream")
        for key, (start, end) in reversed(fields.items()):
            content = content[:start] + json.dumps(defaults[key]) + content[end:]
    else:
        for key, value in defaults.items():
            pattern = r"^(set \$\{si\}\." + key + r"=).*$"
            quoted = uci_batch_quote(value)
            content, count = re.subn(pattern, lambda match: match[1] + quoted, content, flags=re.M)
            if count != 1:
                raise ValueError("mac80211 " + key + " defaults changed upstream")
    width_5g = int(values.get("wifi_5g_width", 80))
    if width_5g not in (80, 160):
        raise ValueError("wifi_5g_width must be 80 or 160")
    # Upstream indents with tabs; tolerate any whitespace so a re-indent cannot
    # silently disable the 5G width cap.
    width_pattern = re.compile(r"(?m)^([ \t]*)else if \(width > 80\)\n[ \t]*width = 80;")
    width_match = width_pattern.search(content)
    if not width_match or len(width_pattern.findall(content)) != 1:
        raise ValueError("mac80211 channel width defaults changed upstream")
    indent = width_match[1]
    body_indent = re.match(r"[ \t]*", width_match[0].split("\n", 1)[1])[0]
    width_override = (
        indent + 'else if (band_name == "5G" && width > ' + str(width_5g) + ")\n"
        + body_indent + "width = " + str(width_5g) + ";\n"
        + width_match[0].replace("width > 80", 'band_name != "5G" && width > 80', 1)
    )
    content = content[:width_match.start()] + width_override + content[width_match.end():]
    country = str(values.get("wifi_country", "CN")).upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("wifi_country must be a two-letter country code")
    # Accept both `country || 'US'` and the newer `uc(configured_country || 'US')`.
    # The fallback variable is `country` in old wifi-scripts and
    # `configured_country` in current ones; accept any identifier ending in it.
    country_pattern = re.compile(r"\b(\w*country)\s*\|\|\s*'[A-Z0-9]{2}'")
    if len(country_pattern.findall(content)) != 1:
        raise ValueError("mac80211 country fallback changed upstream")
    content = country_pattern.sub(lambda match: match[1] + " || '" + country + "'", content)
    return content


def apply_defaults(tree, values, vendor=False, wireless_defaults=None):
    generator = tree / "package/base-files/files/bin/config_generate"
    content, count = re.subn(r"192\.168\.\d+\.\d+", values["lan_ip"], generator.read_text())
    if not count:
        raise ValueError("LAN default generator changed upstream")
    content, count = re.subn(r"hostname='[^']*'", lambda _: "hostname=" + shell_quote(values["hostname"]), content)
    if not count:
        raise ValueError("Hostname default generator changed upstream")
    # Mark only newly generated networks. A late defaults hook must run after
    # vendor network hooks, but must never reset a kept-config sysupgrade.
    anchor = "if [ ! -s /etc/config/network ]; then\n"
    if content.count(anchor) != 1:
        raise ValueError("Fresh network generation guard changed upstream")
    content = content.replace(anchor, anchor + "\ttouch /etc/.project-ipv6-defaults-pending || exit 1\n")
    generator.write_text(content)
    if vendor and wireless_defaults == "mtwifi-shell":
        path = tree / "package/mtk/applications/mtwifi-cfg/files/mtwifi.sh"
        content = path.read_text()
        for key, value in (("ssid", uci_batch_quote(values["wifi_ssid"])),
                           ("encryption", uci_batch_quote(values["wifi_encryption"]))):
            pattern = r"(?m)^([ \t]*set wireless\.default_\$\{dev\}\." + key + r"=).*$"
            replacement = lambda m: m[1] + value
            if key == "encryption":
                def replacement(match):
                    indent = re.match(r"[ \t]*", match[1])[0]
                    return (match[1] + value + "\n" + indent + "set wireless.default_${dev}.key=" +
                            uci_batch_quote(values["wifi_password"]) + "\n" + indent +
                            "set wireless.default_${dev}.disabled=" + str(int(values["wifi_disabled"])))
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
        path.write_text(rewrite_mac80211_defaults(path.read_text(), values))
    overlay = tree / "files/etc/uci-defaults"
    overlay.mkdir(parents=True, exist_ok=True)
    for original in (ROOT / "Scripts/uci-defaults").iterdir():
        shutil.copy2(original, overlay / original.name)
    if values.get("network_tuning"):
        # Boot sysctl applies fq before netifd creates interfaces, not in uci-defaults.
        sysctl = tree / "files/etc/sysctl.d"
        sysctl.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "Scripts/Files/network-tuning.conf", sysctl / "11-project-network.conf")
    # Only system settings; never overwrite restored wireless/network configuration.
    script = "#!/bin/sh\nuci set system.@system[0].hostname=" + shell_quote(values["hostname"]) + "\n"
    script += "uci set system.@system[0].timezone=" + shell_quote(values["timezone"]) + "\n"
    script += "uci set system.@system[0].zonename=" + shell_quote(values["zonename"]) + "\nuci commit system\n"
    script += "uci set luci.main.mediaurlbase=" + shell_quote("/luci-static/" + values["theme"]) + "\nuci commit luci\nexit 0\n"
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
                profile_id=lock["profile_id"], fingerprint=lock["fingerprint"],
                source_commit=lock["source"]["commit"], ci_commit=os.environ.get("GITHUB_SHA", "local"),
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


def cache_source_inputs(tree, paths):
    # Hash tracked contents, including local patches, but not generated conf/.o files.
    files = run("git", "ls-files", "--stage", "-z", "--", *paths, cwd=tree).split("\0")
    digest = hashlib.sha256()
    for entry in sorted(filter(None, files)):
        _, name = entry.split("\t", 1)
        path = tree / name
        digest.update(entry.encode() + b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode() + b"\0")
            if not path.resolve().is_relative_to(tree.resolve()):
                raise ValueError("Cache input symlink escapes source: " + name)
        if not path.is_file():
            digest.update(b"missing\0")
            continue
        digest.update(str(path.stat().st_mode & 0o111).encode() + b"\0")
        update_file_hash(digest, path)
        digest.update(b"\0")
    if not any(files):
        raise ValueError("No tracked source files for cache fingerprint")
    return digest.hexdigest()


def kernel_patchver(tree, target, subtarget, values=None):
    """Resolve literal versions; use the conservative cache path for Make logic."""
    selected = "KERNEL_TESTING_PATCHVER" if values and values.get("TESTING_KERNEL") == "y" else "KERNEL_PATCHVER"
    version = None
    for relative in (target + "/Makefile", target + "/" + subtarget + "/target.mk"):
        path = tree / relative
        if not path.is_file():
            continue
        conditional_depth = 0
        for line in path.read_text().splitlines():
            code = line.split("#", 1)[0].strip()
            if re.match(r"(?:ifeq|ifneq|ifdef|ifndef)\b", code):
                conditional_depth += 1
            elif re.match(r"endif\b", code):
                conditional_depth = max(0, conditional_depth - 1)
            elif re.match(r"(?:override\s+)?define\s+" + selected + r"\b", code):
                return None
            match = re.match(r"(?:(?:override|export)\s+)*" + selected + r"\s*([?:+!])?=\s*(.*?)\s*$", code)
            if not match or (match[1] == "?" and version is not None):
                continue
            if conditional_depth or match[1] in ("+", "!") or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", match[2]):
                return None
            version = match[2]
    return version


def toolchain_source_paths(tree, target, subtarget, values=None):
    paths = ["tools", "toolchain", "include", "config", "scripts", "Makefile", "rules.mk",
             "target/linux/Makefile", target + "/Makefile", target + "/" + subtarget + "/target.mk"]
    generic = "target/linux/generic"
    patchver = kernel_patchver(tree, target, subtarget, values)
    if not patchver:
        return paths + [generic]
    paths += [generic + "/Makefile", generic + "/kernel-" + patchver,
              generic + "/config-" + patchver, generic + "/config-default", generic + "/config-filter"]
    for name in ("backport", "pending", "hack"):
        versioned = generic + "/" + name + "-" + patchver
        paths.append(versioned if (tree / versioned).is_dir() else generic + "/" + name)
    for name in ("files", "files-" + patchver):
        path = generic + "/" + name
        if (tree / path).is_dir():
            paths.append(path)
    return paths


def cache_keys(lock, tree, host):
    values = merge_config([(tree / ".config").read_text()])
    if not values:
        raise ValueError("Cache planning requires the expanded build configuration")
    profile = lock["profile"]
    platform = lock["platform"]
    group = ccache_group(lock["profile_id"], profile, platform)
    target = "target/linux/" + platform["target"]
    platform_files = (target + "/Makefile", target + "/" + platform["subtarget"] + "/target.mk")
    missing = [path for path in platform_files if not (tree / path).is_file()]
    if missing:
        raise ValueError("Toolchain cache input is missing: " + ", ".join(missing))
    selected = toolchain_config(values)
    if not selected:
        raise ValueError("Toolchain cache planning found no target/toolchain configuration")
    # Toolchain kernel headers use their own patches; device DTS, image and kernel
    # configuration changes must not invalidate the shared toolchain cache.
    paths = toolchain_source_paths(tree, target, platform["subtarget"], values)
    if "target/linux/generic" in paths:
        # Unknown Make expressions can select a kernel through any option.
        # Hash all expanded selections as well as all generic kernel sources.
        selected = values
    source_hash = cache_source_inputs(tree, paths)
    foundation = dict(cache_group=group,
                      source={k: lock["source"][k] for k in ("repository", "branch")},
                      platform=(platform["target"], platform["subtarget"]),
                      config=selected, source_hash=source_hash,
                      host=host, directory=str(tree.resolve()))
    toolchain_key = "toolchain-v2-Linux-ubuntu24-" + group + "-" + fingerprint(foundation)[:24]
    # Package host tools retain their feed, overlay, configuration and recipe guards.
    scripts = {str(path.relative_to(ROOT)): file_hash(path)
               for path in sorted((ROOT / "Scripts").rglob("*"))
               if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}
    packages = dict(toolchain_key=toolchain_key, feeds=lock["source"]["feeds"], packages=lock["packages"],
                    config=values, recipes=run("git", "rev-parse", "HEAD:package", cwd=tree), scripts=scripts)
    hostpkg_key = "hostpkg-v2-Linux-ubuntu24-" + lock["profile_id"] + "-" + fingerprint(packages)[:24]
    return dict(toolchain_key=toolchain_key, hostpkg_key=hostpkg_key)


def cache_plan(args):
    lock, tree = read_lock(args.lock), Path(args.source)
    # Arbitrary private hooks can alter build inputs outside the tracked recipe set.
    enabled = not (ROOT / "Scripts/PRIVATE.sh").is_file()
    keys = dict(toolchain_key="", hostpkg_key="")
    if enabled:
        installed = run("dpkg-query", "-W", "-f=${Package}=${Version}\n")
        host = dict(image=os.environ.get("ImageVersion", "unknown"), compiler=run("gcc", "--version"),
                    linker=run("ld", "--version"),
                    packages="\n".join(sorted(installed.splitlines())),
                    dependencies=file_hash(ROOT / "Scripts/install-dependencies.sh"))
        keys = cache_keys(lock, tree, host)
    result = dict(**keys, enabled=str(enabled).lower())
    with open(args.github_output, "a") as output:
        for key, value in result.items():
            output.write(key + "=" + value + "\n")
    write_json(ROOT / "logs/cache-plan.json", result)
    print(json.dumps(result))


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


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def clear_target_outputs(tree):
    # `make clean` also deletes staging_dir, including the restored host,
    # hostpkg and toolchain caches. Remove only outputs that are unsafe to reuse.
    for relative in ("build_dir", "bin", "staging_dir/packages", ".build-complete.json"):
        remove_path(tree / relative)
    for path in (tree / "staging_dir").glob("target-*"):
        remove_path(path)


def cache_refresh(args):
    tree = Path(args.source).resolve()
    refresh_toolchain_cache(tree)
    clear_target_outputs(tree)


def prepare(args):
    lock = read_lock(args.lock)
    if lock["ci_hash"] != ci_hash():
        raise ValueError("CI files changed since planning")
    tree = Path(args.source).resolve()
    if run("git", "-C", str(tree), "rev-parse", "HEAD") != lock["source"]["commit"]:
        raise ValueError("Upstream SHA does not match plan")
    (tree / ".build-complete.json").unlink(missing_ok=True)
    (tree / "feeds.conf").write_text("".join(
        f"{feed['kind']} {feed['name']} {feed['repository']}^{feed['commit']}\n"
        for feed in lock["source"]["feeds"]
    ))
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
        platform = lock["platform"]
        env = {**os.environ, "GITHUB_WORKSPACE": str(ROOT), "WRT_PROFILE": lock["profile_id"],
               "WRT_TARGET": platform["target"], "WRT_SUBTARGET": platform["subtarget"],
               "WRT_IP": lock["defaults"]["lan_ip"], "WRT_MARK": identity["repository_owner"],
               "WRT_DATE": identity["build_date"], "WRT_BRANCH": lock["source"]["branch"],
               "WRT_INFO": lock["profile"]["source"]}
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        run("bash", str(private), cwd=tree, env=env)
    remove_qmodem(tree)
    vendor = lock["source"].get("vendor_wireless", False)
    apply_defaults(tree, lock["defaults"], vendor, lock["source"].get("wireless_defaults"))
    profile_patches = lock["profile"].get("patches", [])
    apply_source_patches(tree, profile_patches)
    apply_ustream_ssl_write_patch(tree)
    apply_kernel_config(tree, lock["profile"])
    customize_luci(tree, lock["defaults"], identity)
    write_json(tree / ".build-identity.json", identity)
    write_json(tree / "files/usr/share/openwrt-build/build-info.json", identity)
    values = lock["config"]
    content = serialize_config(values)
    (tree / ".config").write_text(content)
    run("make", "defconfig", cwd=tree)
    actual = (tree / ".config").read_text()
    validate_config(lock["profile_id"], lock["profile"], actual, values)
    print("Configuration verified: " + lock["profile_id"])


def image_manifest(target, profile, profile_id=None, platform=None):
    platform = platform or profile_platform(profile)
    manifests = sorted(p for p in target.glob("*.manifest") if p.is_file())
    device_suffix = "-" + platform["device"] + ".manifest"
    platform_suffix = "-" + platform["target"] + "-" + platform["subtarget"]
    # Older CI exported PROFILE, which upstream used as a manifest suffix.
    # Match only this locked profile, then the normal target-wide manifest.
    suffixes = [device_suffix]
    if profile_id:
        suffixes.append(platform_suffix + "-" + profile_id + ".manifest")
    suffixes.append(platform_suffix + ".manifest")
    candidates = []
    for suffix in suffixes:
        candidates = [p for p in manifests if p.name.endswith(suffix)]
        if candidates:
            break
    if len(candidates) != 1:
        found = ", ".join(p.name for p in manifests) or "none"
        raise ValueError("Expected one matching package manifest; found: " + found)
    return candidates[0]


def validate_image_packages(target, profile, profile_id=None, platform=None):
    manifest = image_manifest(target, profile, profile_id, platform)
    print("Package manifest found: " + manifest.name)
    return manifest


def seven_dr_images(target, profile, platform=None):
    platform = platform or profile_platform(profile)
    images = []
    available = sorted(path for path in target.glob("*.itb") if path.is_file())
    # Final filenames include OpenWrt's squashfs/initramfs prefixes.
    suffixes = {"sysupgrade": ("-squashfs-sysupgrade.itb", "-sysupgrade.itb"),
                "recovery": ("-initramfs-recovery.itb", "-recovery.itb")}
    for kind, endings in suffixes.items():
        candidates = [path for path in available
                      if any(path.name.endswith("-" + platform["device"] + ending) for ending in endings)]
        if len(candidates) != 1:
            matched = ", ".join(path.name for path in candidates) or "none"
            found = ", ".join(path.name for path in available) or "none"
            raise ValueError(f"Expected one 7DR7299 {kind}.itb image; matched {len(candidates)}: {matched}; available: {found}")
        images.extend(candidates)
    return images


def validate_jdcloud_sysupgrade(path, tree, compression="lzma"):
    # Check the shipped FIT, not just the source recipe; never enlarge the kernel slot.
    if compression not in ("lzma", "gzip"):
        raise ValueError("Invalid RE-CS-06 kernel compression")
    with tempfile.TemporaryDirectory(prefix="owrt-kernel-check-") as temporary:
        kernel = Path(temporary) / "kernel.itb"
        with tarfile.open(path) as archive:
            members = [m for m in archive.getmembers() if m.isfile() and m.name.endswith("/kernel")]
            if len(members) != 1:
                raise ValueError("Expected one RE-CS-06 sysupgrade kernel")
            size = members[0].size
            limit = 7168 * 1024
            if not 0 < size <= limit:
                raise ValueError(f"RE-CS-06 FIT kernel exceeds 7 MiB or is empty: {size} bytes")
            with archive.extractfile(members[0]) as source, kernel.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        listing = run(str(tree / "staging_dir/host/bin/mkimage"), "-l", str(kernel))
        compressions = re.findall(r"(?m)^\s*Compression:\s*(.+?)\s*$", listing)
        if not listing.startswith("FIT description:") or not compressions or compressions[0] != compression + " compressed":
            raise ValueError(f"Expected a {compression.upper()}-compressed RE-CS-06 FIT kernel")
    print(f"RE-CS-06 FIT kernel verified: {size} / {limit} bytes; {compression.upper()}")


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


def manifest_summary(manifest):
    kernel = None
    luci = set()
    for line in Path(manifest).read_text().splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[1] != "-":
            continue
        name = fields[0]
        if name == "kernel":
            kernel = fields[2]
        elif name.startswith(("luci-app-", "luci-theme-")):
            luci.add(name)
    return dict(kernel=kernel, luci=sorted(luci))


def release_notes(lock, identity=None, config_name="Config-*.txt", manifest=None):
    profile, values, source = lock["profile"], lock["defaults"], lock["source"]
    platform = lock.get("platform") or profile_platform(profile)
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
    if platform["target"] != "x86":
        notes += "| Wi-Fi | `" + values["wifi_ssid"] + "` · `" + values["wifi_password"] + "` |\n"
    else:
        notes += "\nx86 无内置无线时不生成热点。\n"
    notes += "\n> 首次登录请修改默认密码。只刷匹配机型与分区的镜像；sysupgrade 不代表适用于原厂首次刷入，勿强刷。\n"
    if manifest:
        summary = manifest_summary(manifest)
        notes += "\n### 固件摘要\n\n| 项目 | 内容 |\n| :--- | :--- |\n"
        notes += "| 内核版本 | `" + (summary["kernel"] or "未识别") + "` |\n"
        notes += "| LuCI 应用 / 主题 | " + str(len(summary["luci"])) + " 项 |\n"
        if summary["luci"]:
            notes += ("\n<details>\n<summary>LuCI 应用与主题</summary>\n\n"
                      + "\n".join("- `" + name + "`" for name in summary["luci"])
                      + "\n\n</details>\n")
    if lock["profile_id"] == "jdcloud-re-cs-06":
        compression = profile.get("kernel_compression", "lzma").upper()
        notes += (f"\n> RE-CS-06 适配 2G 大分区，使用 {compression}/FIT 内核并保留上游 7 MiB 内核区域与 eMMC 布局，"
                  "刷入前请确认引导器兼容。\n")
    if lock["profile_id"].startswith("s20p-"):
        notes += "\n> S20P MTK / MT76 不保证直接互刷。"
        if profile["source"] == "chasey":
            notes += "HNAT / WARP 与透明代理需验证兼容性。"
        notes += "\n"
    if lock["profile_id"].startswith("7dr7299-"):
        notes += "\n> 仅适用于 TP-Link TL-7DR7299 v1。MT76 / MTK 的引导器及分区布局需分别核对，不保证直接互刷。"
        notes += "sysupgrade.itb 用于匹配布局的 OpenWrt 升级；recovery.itb 为恢复镜像，不是原厂首次刷入包；不发布引导器。"
        if source.get("vendor_wireless", False):
            notes += "闭源版不集成 Mesh / usteer；HNAT / WARP 相关功能使用前需验证兼容性。"
        notes += "\n"
    notes += "\n<details>\n<summary>源码与文件</summary>\n\n"
    notes += "源码：[" + source["branch"] + " · " + source["commit"][:7] + "](" + repository + "/commit/" + source["commit"] + ")。\n\n"
    notes += "校验：`sha256sums` · 完整配置：`" + config_name + "` · 包清单：`*.manifest` · 构建记录：`build-info.json`。\n\n"
    notes += "其余构建诊断保存在 Actions 的 logs 附件中。\n\n</details>\n\n"
    notes += "<!-- build-fingerprint:" + lock["fingerprint"] + " -->\n"
    if "firmware_fingerprint" in lock:
        notes += firmware_marker(lock["firmware_fingerprint"]) + "\n"
    notes += "<!-- build-ci:" + lock["ci_hash"] + " -->\n"
    notes += config_marker(lock["config"]) + "\n"
    return notes + source_marker(source) + "\n"


def collect(args):
    lock = read_lock(args.lock)
    tree = Path(args.source)
    output = Path(args.output_dir)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("Collection output must be an empty directory")
    profile = lock["profile"]
    platform = lock.get("platform") or profile_platform(profile)
    target = tree / "bin/targets" / platform["target"] / platform["subtarget"]
    validate_config(lock["profile_id"], profile, (tree / ".config").read_text(), lock["config"])
    identity = read_json(tree / ".build-identity.json")
    if identity["profile_id"] != lock["profile_id"]:
        raise ValueError("Build identity profile mismatch")
    if (identity.get("fingerprint") != lock["fingerprint"]
            or identity.get("source_commit") != lock["source"]["commit"]):
        raise ValueError("Build identity does not match the locked source and configuration")
    completion = tree / ".build-complete.json"
    if not completion.is_file() or read_json(completion) != identity:
        raise ValueError("No successful compilation for this build. Run Scripts/compile.sh after prepare.")
    manifest = validate_image_packages(target, profile, lock["profile_id"], platform)
    if lock["profile_id"] == "jdcloud-re-cs-06":
        sysupgrades = list(target.glob("*" + platform["device"] + "*sysupgrade*.bin"))
        if len(sysupgrades) != 1:
            raise ValueError("Expected one RE-CS-06 sysupgrade image")
        validate_jdcloud_sysupgrade(sysupgrades[0], tree, profile.get("kernel_compression", "lzma"))
    fit_images = seven_dr_images(target, profile, platform) if lock["profile_id"].startswith("7dr7299-") else None
    if lock["profile_id"] == "s20p-mtk":
        sysupgrades = list(target.glob("*" + platform["device"] + "*sysupgrade*.bin"))
        if len(sysupgrades) != 1:
            raise ValueError("Expected one S20P MTK sysupgrade image")
        validate_mtk_sysupgrade(sysupgrades[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + output.name + "-", dir=output.parent) as temporary:
        staged = Path(temporary)
        collect_assets(lock, tree, staged, target, manifest, identity, fit_images)
        staged.replace(output)


def collect_assets(lock, tree, output, target, manifest, identity, fit_images):
    """Assemble and checksum a complete release in its temporary output directory."""
    profile = lock["profile"]
    platform = lock.get("platform") or profile_platform(profile)
    variant = "-" + profile.get("kernel_compression", "lzma") if lock["profile_id"] == "jdcloud-re-cs-06" else ""
    stamp = lock["profile_id"] + variant + "-" + identity["build_date"] + "-r" + identity["run_id"] + "-" + identity["run_attempt"]
    diagnostics = ROOT / "logs/metadata"
    diagnostics.mkdir(parents=True, exist_ok=True)
    images = []
    for path in sorted(target.iterdir()):
        if not path.is_file():
            continue
        is_image = platform["device"] in path.name and path.name.endswith(IMAGE_EXTENSIONS) and (
            any(s in path.name for s in ("sysupgrade", "factory", "initramfs")) or
            (platform["target"] == "x86" and re.search(r"\.(img(?:\.gz)?|vmdk|vdi|qcow2)$", path.name)))
        if fit_images is not None:
            is_image = path in fit_images
        if is_image:
            if path.is_symlink() or path.stat().st_size == 0:
                raise ValueError("Firmware image must be a nonempty regular file: " + path.name)
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
    (diagnostics / "config.seed").write_text(serialize_config(lock["config"]))
    (diagnostics / "config.diff").write_text(run("./scripts/diffconfig.sh", cwd=tree) + "\n")
    assets = sorted(p.name for p in output.iterdir()) + ["build-info.json", "sha256sums"]
    info = {**{k: v for k, v in lock.items() if k not in ("config", "requested_packages")}, **identity,
            "images": images, "release_assets": assets,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    write_json(output / "build-info.json", info)
    (output / "release-notes.md").write_text(release_notes(lock, identity, config_name, manifest))
    checksums = []
    for name in sorted(assets):
        path = output / name
        if name != "sha256sums":
            checksums.append(file_hash(path) + "  " + path.name + "\n")
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
    p.add_argument("--kernel-compression", choices=("profile", "lzma", "gzip"), default="profile")
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
    p = sub.add_parser("cache-plan")
    p.add_argument("--source", required=True)
    p.add_argument("--lock", required=True)
    p.add_argument("--github-output", required=True)
    p = sub.add_parser("cache-refresh")
    p.add_argument("--source", required=True)
    p = sub.add_parser("checkout")
    p.add_argument("--source", required=True)
    p.add_argument("--lock", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--fingerprint", required=True)
    args = parser.parse_args()
    {"plan": plan, "checkout": checkout_locked, "prepare": prepare, "collect": collect,
     "cache-plan": cache_plan, "cache-refresh": cache_refresh}[args.command](args)


if __name__ == "__main__":
    main()
