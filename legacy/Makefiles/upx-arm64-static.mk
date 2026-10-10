include $(TOPDIR)/rules.mk

PKG_NAME:=upx-arm64-static
PKG_VERSION:=5.2.1
PKG_RELEASE:=1
PKG_SOURCE:=upx-$(PKG_VERSION)-arm64_linux.tar.xz
PKG_SOURCE_URL:=https://github.com/upx/upx/releases/download/v$(PKG_VERSION)
PKG_HASH:=a72d112c5970a904a31da0b9c84f919bc16b9a311787c12245508544a78c7d36
PKG_LICENSE:=GPL-2.0-or-later
PKG_LICENSE_FILES:=COPYING

include $(INCLUDE_DIR)/package.mk

define Package/upx-arm64-static
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Official UPX 5.2.1 static ARM64 executable
  URL:=https://github.com/upx/upx
  DEPENDS:=@aarch64
endef

define Build/Prepare
	$(INSTALL_DIR) $(PKG_BUILD_DIR)
	tar -xJf $(DL_DIR)/$(PKG_SOURCE) -C $(PKG_BUILD_DIR) --strip-components=1
	test -s $(PKG_BUILD_DIR)/upx
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/upx-arm64-static/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/upx $(1)/usr/bin/upx
endef

$(eval $(call BuildPackage,upx-arm64-static))
