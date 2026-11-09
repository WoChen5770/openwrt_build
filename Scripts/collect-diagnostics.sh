#!/usr/bin/env bash
set -uo pipefail

source_dir=${1:?source directory is required}
log_dir=${2:?log directory is required}
mkdir -p "$log_dir"
rm -f "$log_dir/source-commit.txt" "$log_dir/source-commit-summary.txt"

summary="$log_dir/build-summary.md"
host_tools="$log_dir/host-tools.txt"
toolchain_tools="$log_dir/toolchain-after-build.txt"
build_time_reports=()

{
  echo "runner: ${ImageOS:-unknown} ${ImageVersion:-unknown}"
  echo "kernel: $(uname -srm 2>/dev/null || echo unknown)"
  for tool in gcc clang ld make perl python3 go rustc cargo ccache; do
    if command -v "$tool" >/dev/null 2>&1; then
      case "$tool" in
        go) version=$("$tool" version 2>/dev/null | head -n 1) ;;
        perl) version=$("$tool" -e 'print $^V' 2>/dev/null) ;;
        *) version=$("$tool" --version 2>/dev/null | head -n 1) ;;
      esac
      echo "$tool: ${version:-version unavailable}"
    else
      echo "$tool: missing"
    fi
  done
} > "$host_tools"

if [ -d "$source_dir" ]; then
  if [ -e "$source_dir/.git" ]; then
    if git -C "$source_dir" rev-parse HEAD > "$log_dir/source-commit.txt" 2>/dev/null; then
      echo "Source commit: $(cat "$log_dir/source-commit.txt")" > "$log_dir/source-commit-summary.txt"
    fi
  fi
  if [ -f "$source_dir/.config" ]; then
    cp "$source_dir/.config" "$log_dir/config.full"
  fi
  if [ -x "$source_dir/scripts/feeds" ]; then
    (cd "$source_dir" && ./scripts/feeds list -fs) > "$log_dir/feeds.buildinfo" 2>&1 || true
  fi
  shopt -s nullglob
  for kernel in "$source_dir"/build_dir/target-*/linux-*/linux-*; do
    [ -f "$kernel/.config" ] || continue
    label="$(basename "$(dirname "$kernel")")-$(basename "$kernel")"
    cp "$kernel/.config" "$log_dir/kernel-config-$label"
    if [ -f "$kernel/vmlinux" ]; then
      readelf -SW "$kernel/vmlinux" > "$log_dir/kernel-sections-$label.log" 2>&1 || true
    fi
  done
  if [ -d "$source_dir/build_dir" ]; then
    find "$source_dir/build_dir" -type f \( -name vmlinux -o -name 'Image*' -o -name '*.itb' -o -name '*.dtb' \) \
      -printf '%s %P\n' 2>/dev/null | sort -k2 > "$log_dir/kernel-sizes.log" || true
  fi
  if [ -d "$source_dir/logs" ]; then
    mkdir -p "$log_dir/upstream"
    cp -a "$source_dir/logs/." "$log_dir/upstream/" 2>/dev/null || true
  fi
  if [ -d "$source_dir/.ccache" ] && command -v ccache >/dev/null 2>&1; then
    CCACHE_DIR="$source_dir/.ccache" ccache --show-stats > "$log_dir/ccache.log" 2>&1 || true
  fi
  {
    for compiler in "$source_dir"/staging_dir/toolchain-*/bin/*-gcc; do
      [ -x "$compiler" ] || continue
      echo "cross gcc: $("$compiler" --version 2>/dev/null | head -n 1)"
      echo "cross gcc path: $(readlink -f "$compiler" 2>/dev/null || echo "$compiler")"
    done
    for host_bin in "$source_dir/staging_dir/host/bin" "$source_dir/staging_dir/hostpkg/bin"; do
      [ -d "$host_bin" ] || continue
      if [ -x "$host_bin/rustc" ]; then
        echo "rustc: $("$host_bin/rustc" -vV 2>/dev/null | head -n 1)"
        echo "rustc path: $(readlink -f "$host_bin/rustc" 2>/dev/null || echo "$host_bin/rustc")"
        if [ -x "$host_bin/cargo" ]; then
          echo "cargo: $("$host_bin/cargo" --version 2>/dev/null | head -n 1)"
        fi
      fi
      for go_bin in "$host_bin"/go "$host_bin"/go1.*; do
        [ -x "$go_bin" ] || continue
        echo "go: $("$go_bin" version 2>/dev/null | head -n 1)"
        echo "go path: $(readlink -f "$go_bin" 2>/dev/null || echo "$go_bin")"
      done
      if [ -x "$host_bin/python3" ]; then
        echo "python: $("$host_bin/python3" --version 2>&1 | head -n 1)"
        "$host_bin/python3" -c 'import sys, sysconfig; print("python executable:", sys.executable); print("python config:", sysconfig.get_config_var("CONFIG_ARGS"))' 2>/dev/null || true
      fi
    done
  } > "$toolchain_tools"
  for tsv in "$log_dir"/build-time-*.tsv; do
    [ -s "$tsv" ] || continue
    report="${tsv%.tsv}-report.txt"
    if [ -f "$source_dir/scripts/build-time-report.pl" ]; then
      (cd "$source_dir" && perl ./scripts/build-time-report.pl -n 30 "$tsv") > "$report" 2>&1 || cp "$tsv" "$report"
    else
      cp "$tsv" "$report"
    fi
    build_time_reports+=("$report")
  done
  cache_paths=()
  for path in "$source_dir/.ccache" "$source_dir/staging_dir/host" "$source_dir/staging_dir/hostpkg" "$source_dir"/staging_dir/toolchain-*; do
    [ -e "$path" ] && cache_paths+=("$path")
  done
  if [ ${#cache_paths[@]} -gt 0 ]; then
    du -sh "${cache_paths[@]}" > "$log_dir/cache-sizes.log" 2>/dev/null || true
  fi
  shopt -u nullglob
fi

{
  echo '### Build diagnostics'
  echo
  echo '```text'
  echo "toolchain cache hit: ${TOOLCHAIN_CACHE_HIT:-not-run}"
  echo "hostpkg cache hit: ${HOSTPKG_CACHE_HIT:-not-run}"
  if [ -s "$log_dir/source-commit-summary.txt" ]; then cat "$log_dir/source-commit-summary.txt"; fi
  if [ -s "$host_tools" ]; then cat "$host_tools"; fi
  if [ -s "$log_dir/ccache.log" ]; then cat "$log_dir/ccache.log"; fi
  if [ -s "$log_dir/cache-sizes.log" ]; then cat "$log_dir/cache-sizes.log"; fi
  if [ -s "$toolchain_tools" ]; then
    echo
    echo '--- toolchain-after-build.txt ---'
    sed -n '1,20p' "$toolchain_tools"
  fi
  for report in "${build_time_reports[@]}"; do
    echo
    echo "--- $(basename "$report") ---"
    sed -n '1,35p' "$report"
  done
  if [ ${#build_time_reports[@]} -eq 0 ]; then
    echo
    echo "build-time report: not produced"
  fi
  echo '```'
} > "$summary"

exit 0
