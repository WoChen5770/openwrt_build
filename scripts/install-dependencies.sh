#!/usr/bin/env bash
set -euo pipefail
# GitHub hosted Ubuntu 24.04 only. No remote init scripts or broad disk deletion.
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential bison ccache clang cmake curl device-tree-compiler file flex \
  g++-multilib gawk gcc-multilib gettext git help2man jq libelf-dev libfuse-dev \
  libgmp-dev libmpc-dev libmpfr-dev libncurses-dev libssl-dev libtool-bin llvm \
  ninja-build p7zip-full patch pkgconf protobuf-compiler python3 python3-setuptools \
  qemu-utils rsync subversion swig time unzip wget xsltproc xz-utils zlib1g-dev zstd
