#!/usr/bin/env bash
set -euo pipefail
source_dir=${1:?source directory is required}
log_dir=${2:?log directory is required}
mkdir -p "$log_dir"
source_dir=$(cd "$source_dir" && pwd)
log_dir=$(cd "$log_dir" && pwd)
cd "$source_dir"
jobs=${BUILD_JOBS:-$(nproc)}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid BUILD_JOBS' >&2; exit 1; }
make download -j"$jobs" 2>&1 | tee "$log_dir/download.log"
# Checksum and make errors are not hidden. Retry serially for useful diagnostics.
parallel_time_log="$log_dir/build-time-parallel.tsv"
serial_time_log="$log_dir/build-time-serial.tsv"
if ! BUILD_TIME_LOG="$parallel_time_log" make -j"$jobs" 2>&1 | tee "$log_dir/build.log"; then
  BUILD_TIME_LOG="$serial_time_log" make -j1 V=s 2>&1 | tee "$log_dir/build-serial.log"
fi
