#!/bin/bash
# Supported proxy profiles: install upstream packages alongside daed, without enabling honk.
set -euo pipefail

case "${WRT_CONFIG:-}" in
	SUPERGATEWAY-S20P-MT76|SUPERGATEWAY-S20P-MTK|X86|TPLINK-7DR7299-MT76|TPLINK-7DR7299-MTK|JDCLOUD-RE-CS-06) ;;
	*) exit 0 ;;
esac

for HONK_ROOT in ./package ./feeds/luci ./feeds/packages; do
	if [ ! -d "$HONK_ROOT" ]; then
		echo "Honk integration must run in the prepared OpenWrt tree: missing $HONK_ROOT" >&2
		exit 1
	fi
done

# Keep the LuCI package and its prebuilt native-api core on the same resolved revision.
HONK_REPO="https://github.com/189160/luci-app-honk.git"
HONK_REF="${HONK_COMMIT:?HONK_COMMIT must come from the build plan}"
HONK_TMP=$(mktemp -d)
trap 'rm -rf -- "$HONK_TMP"' EXIT

git init -q "$HONK_TMP/source"
git -C "$HONK_TMP/source" remote add origin "$HONK_REPO"
git -C "$HONK_TMP/source" fetch --depth=1 origin "$HONK_REF"
git -C "$HONK_TMP/source" checkout -q --detach FETCH_HEAD
test "$(git -C "$HONK_TMP/source" rev-parse HEAD)" = "$HONK_REF"

for HONK_FILE in honk/Makefile honk/files/honk.config honk/files/honk.init \
                 honk/files/config.d/api.dae luci-app-honk/Makefile \
                 luci-app-honk/po/zh_Hans/honk.po; do
	test -f "$HONK_TMP/source/$HONK_FILE"
done

# Validate, rather than rewrite, upstream runtime defaults. Never run the installer
# or version updater: the resolved Makefile already supplies the binary URL and hash.
grep -Eq "^[[:space:]]*option[[:space:]]+enabled[[:space:]]+'0'[[:space:]]*$" \
	"$HONK_TMP/source/honk/files/honk.config"
if grep -Eq '^[[:space:]]*native_api[[:space:]]*\{' "$HONK_TMP/source/honk/files/config.d/api.dae"; then
	echo "Honk native API must remain disabled in the packaged defaults" >&2
	exit 1
fi

# Fetch and validate both packages before replacing only exact-name definitions.
# Do not touch dae, daed, luci-app-daede, PassWall, or existing DNS settings.
find ./package ./feeds/luci ./feeds/packages -maxdepth 3 \
	\( -type d -o -type l \) \( -name honk -o -name luci-app-honk \) \
	-prune -exec rm -rf -- {} +
cp -R "$HONK_TMP/source/honk" ./package/honk
cp -R "$HONK_TMP/source/luci-app-honk" ./package/luci-app-honk
echo "Honk source: $HONK_REPO @ $HONK_REF (prebuilt core; disabled by default)"
