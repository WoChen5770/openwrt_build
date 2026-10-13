include $(TOPDIR)/rules.mk

PKG_NAME:=xray-core
PKG_VERSION:=@XRAY_VERSION@
PKG_RELEASE:=1

# Resolved for each build, including prereleases. Asset ID avoids stale re-upload caches.
PKG_SOURCE:=@XRAY_SOURCE@
PKG_SOURCE_URL:=https://github.com/XTLS/Xray-core/releases/download/@XRAY_TAG@
PKG_SOURCE_URL_FILE:=@XRAY_ASSET@
PKG_HASH:=@XRAY_HASH@

PKG_LICENSE:=MPL-2.0
PKG_LICENSE_FILES:=LICENSE

include $(INCLUDE_DIR)/package.mk

define Package/xray-core
  SECTION:=net
  CATEGORY:=Network
  TITLE:=Xray core (official @XRAY_ARCH_LABEL@ binary)
  URL:=https://github.com/XTLS/Xray-core
  DEPENDS:=@XRAY_ARCH_DEPENDS@ +ca-bundle
endef

define Package/xray-core/description
 Official XTLS @XRAY_ARCH_LABEL@ release, packaged at /usr/bin/xray for PassWall.
 Geodata remains managed by the existing OpenWrt/PassWall packages.
endef

define Build/Prepare
	$(INSTALL_DIR) $(PKG_BUILD_DIR)
	unzip -o $(DL_DIR)/$(PKG_SOURCE) xray LICENSE -d $(PKG_BUILD_DIR)
	test -s $(PKG_BUILD_DIR)/xray
endef

define Build/Configure
endef

define Build/Compile
endef

define Package/xray-core/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/xray $(1)/usr/bin/xray
endef

$(eval $(call BuildPackage,xray-core))
