include $(TOPDIR)/rules.mk

PKG_NAME:=owrt-core-update
PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT

include $(INCLUDE_DIR)/package.mk

define Package/owrt-core-update
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Manual verified UPX core updater for BE12 Pro
  DEPENDS:=@aarch64 +curl +ca-bundle +jsonfilter +unzip +upx-arm64-static
  PKGARCH:=all
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/owrt-core-update/install
	$(INSTALL_DIR) $(1)/usr/sbin
	$(INSTALL_BIN) ./files/owrt-core-update $(1)/usr/sbin/owrt-core-update
endef

$(eval $(call BuildPackage,owrt-core-update))
