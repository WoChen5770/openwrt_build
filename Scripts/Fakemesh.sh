#!/bin/bash
# Supported open-driver profiles: install a pinned FakeMesh snapshot.
set -euo pipefail

case "${WRT_CONFIG:-}" in
	TENDA-BE12-PRO|JDCLOUD-RE-CS-06) ;;
	*) exit 0 ;;
esac

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE must point to the CI repository}"
for FAKEMESH_ROOT in ./package ./feeds/luci ./feeds/packages; do
	if [ ! -d "$FAKEMESH_ROOT" ]; then
		echo "FakeMesh integration must run in the prepared OpenWrt tree: missing $FAKEMESH_ROOT" >&2
		exit 1
	fi
done

FAKEMESH_REPO="https://github.com/x-wrt/com.x-wrt.git"
FAKEMESH_REF="${FAKEMESH_COMMIT:?FAKEMESH_COMMIT must come from the build plan}"
FAKEMESH_TMP=$(mktemp -d)
trap 'rm -rf -- "$FAKEMESH_TMP"' EXIT

git init -q "$FAKEMESH_TMP/source"
git -C "$FAKEMESH_TMP/source" remote add origin "$FAKEMESH_REPO"
git -C "$FAKEMESH_TMP/source" fetch --depth=1 origin "$FAKEMESH_REF"
git -C "$FAKEMESH_TMP/source" checkout -q --detach FETCH_HEAD
test "$(git -C "$FAKEMESH_TMP/source" rev-parse HEAD)" = "$FAKEMESH_REF"

for FAKEMESH_FILE in luci-app-fakemesh/Makefile \
		luci-app-fakemesh/root/etc/uci-defaults/96-fakemesh-defaults \
		luci-app-fakemesh/root/usr/sbin/fakemesh \
		luci-app-fakemesh/root/usr/libexec/rpcd/luci.fakemesh \
		luci-app-fakemesh/htdocs/luci-static/resources/view/fakemesh/fakemesh.js \
		luci-app-fakemesh/htdocs/luci-static/resources/view/fakemesh/topology.js; do
	test -f "$FAKEMESH_TMP/source/$FAKEMESH_FILE"
done

patch --batch --forward --fuzz=0 -d "$FAKEMESH_TMP/source" -p1 \
	< "$GITHUB_WORKSPACE/Scripts/Patches/fakemesh-immortalwrt.patch"

grep -q '^PKG_BUILD_DEPENDS:=luci-base/host$' "$FAKEMESH_TMP/source/luci-app-fakemesh/Makefile"

find ./package ./feeds/luci ./feeds/packages -maxdepth 3 \
	\( -type d -o -type l \) -name luci-app-fakemesh -prune -exec rm -rf -- {} +
cp -R "$FAKEMESH_TMP/source/luci-app-fakemesh" ./package/luci-app-fakemesh
echo "FakeMesh source: $FAKEMESH_REPO @ $FAKEMESH_REF (X-WRT defaults; disabled by default)"
