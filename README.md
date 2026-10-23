<div align="center">

# ImmortalWrt Build

多机型支持 · 自动构建 · 独立发布

[![Build](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml/badge.svg)](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/WoChen5770/openwrt_build?style=flat&color=2563eb)](https://github.com/WoChen5770/openwrt_build/releases)

[下载固件](https://github.com/WoChen5770/openwrt_build/releases) · [在线构建](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml) · [自定义配置](#自定义)

</div>

## 选择机型

| 机型 | 特性与插件 | 源码 |
| :--- | :--- | :--- |
| x86-64 | BBR · BIOS / UEFI / VMDK<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| TL-7DR7299 · MT76 | 开源无线 · Mesh / DAWN · BBR / fq<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| TL-7DR7299 · MTK | 闭源无线 · HNAT / WARP · TurboACC / eQoS<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [padavanonly / 6.6-mtwifi](https://github.com/padavanonly/immortalwrt-mt798x-6.6/tree/mt798x-mt799x-6.6-mtwifi) |
| Xiaomi BE6500 | ath12k · PPE · Mesh / DAWN · BBR / fq | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| Tenda BE12 Pro | MT76 · Mesh / DAWN · BBR / fq | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| 超级网关S20P · MT76 | 开源无线 · Mesh · BBR / fq<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| 超级网关S20P · MTK | 闭源无线 · HNAT / WARP · TurboACC / eQoS<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [chasey-dev / 25.12](https://github.com/chasey-dev/immortalwrt-mt798x-rebase/tree/25.12) |
| 京东云无线宝赵云 · JDCloud RE-CS-06 | ath12k · PPE · Mesh / DAWN · BBR / fq<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |

## 默认设置

| 项目 | 设置 |
| :--- | :--- |
| 管理入口 | `192.168.123.1` · HTTP / HTTPS |
| 管理账号 | `root` · 默认空密码 |
| Wi-Fi | `ImmortalWrt` · 密码 `12345678` |
| 界面 | shadcn · 简体中文 |
| 时区 | Asia/Shanghai |


## 构建与发布

| 项目 | 说明 |
| :--- | :--- |
| 手动构建 | [Run workflow](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml) · 单机型或 `all` 并行 |
| 自动构建 | 每日北京时间 **04:15** 检查主源码或本仓库配置/脚本，有更新才构建 |
| 缓存加速 | 按兼容组共享 ccache 与 host / 工具链，机型独立 hostpkg；下载文件不缓存 |
| 固件发布 | 同一工作流内默认发布成功机型的 Release；可通过 `publish=false` 关闭 |
| 版本标识 | LuCI 固件版本显示仓库所有者、北京时间（编译开始）；镜像文件名带机型、同一时间和运行编号 |
| 升级页面 | 恢复出厂 / 不保留配置升级后重连默认管理 IP；保留配置升级仍沿用当前地址 |
| 自动清理 | 构建与发布流程结束后自动执行，每日北京时间 **02:30** 兜底；每机型保留 1 个正式 Release，缓存按组 / 机型及类型保留 1 代，每个 workflow 保留最近 3 次运行记录（便于对比构建） |


### 工作流

| 入口 | 功能 |
| :--- | :--- |
| [Build firmware](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml) | 锁定源码与配置、并行编译、筛选成功产物、校验并发布 Release |
| [Clean old releases, caches and workflow runs](https://github.com/WoChen5770/openwrt_build/actions/workflows/cleanup.yml) | 清理本项目可识别的旧 Release / 标签、缓存和 Actions 运行记录；支持手动预览 |
| [Repository checks](https://github.com/WoChen5770/openwrt_build/actions/workflows/check.yml) | 检查配置、校验脚本和 Actions 语法，不编译固件 |


## 目录结构

```text
.github/workflows/   构建（含发布）、清理与检查三个入口
Config/              独立机型配置和 TOML 设置
Scripts/             构建脚本、插件集成及配套资源
```

## 自定义

<details>
<summary>配置文件与进阶选项</summary>

| 配置文件 | 用途 |
| :--- | :--- |
| `Config/<机型>.txt` | 每机型一份完整配置，直接修改，无公共片段 |
| [Config/profiles.toml](Config/profiles.toml) | 机型、源码、配置路径与硬件校验 |
| [Config/packages.toml](Config/packages.toml) | 插件源码与适用机型 |
| [Config/firmware-defaults.toml](Config/firmware-defaults.toml) | 网络、主题、时区 |
| [Config/build-settings.toml](Config/build-settings.toml) | 并发、自动构建、保留周期 |

| 进阶选项 | 说明 |
| :--- | :--- |
| `source_commit` | 单机型锁定上游完整 40 位 SHA |
| `kernel_compression` | 仅赵云支持覆盖：`profile` 使用默认 LZMA；`lzma` / `gzip` 可分别发起构建，按压缩方式隔离并发组；两组对比建议锁定同一 `source_commit` 并设 `publish=false` |
| Python | 构建脚本需要 3.11+，Actions 已满足 |
| `packages` | 本次构建追加选包，优先级高于配置文件；只能填写 `CONFIG_PACKAGE_name=y/m/n` |
| `Config/PRIVATE.txt` | 可选的最后一层配置，应用到所选机型（`all` 时是全部机型） |
| `Scripts/PRIVATE.sh` | 可选 Bash 钩子，在锁定插件集成之后、默认设置 / QModem 清理之前执行；工作目录为 OpenWrt 源码根目录 |
| ccache | 暂不设容量上限；严格校验编译器内容，命中统计见 `logs/ccache.log` 与 Actions Summary |
| 下载文件 | 不缓存 `dl`；每次构建通过 `make download` 并行下载，日志见 `logs/download.log` |
| host / 工具链缓存 | 同一源码、target / subtarget 的机型共享；指纹只包含工具链相关源码和配置 |
| hostpkg 缓存 | 独立保存软件包宿主工具，额外校验 feeds、插件、配置与集成脚本 |
| 7DR7299 MTK 的 Go | 仅 dae / daed 使用锁定的独立 Go 1.27 工具链；其他软件包继续使用上游 Go |
| MTK 服务 | 不集成 Mesh；daed / honk 已集成，默认不启用 |

临时选包示例（可多行，也可用字面量 `\n` 分隔）：

```text
CONFIG_PACKAGE_luci-app-samba4=y
CONFIG_PACKAGE_samba4=y
```
</details>

### 赵云配置与压缩对比

赵云保留 ath12k / PPE、Mesh / DAWN、PassWall（Xray-only）、daed / honk、EasyTier、AdGuard Home、shadcn 以及 BBR / fq；保留 BTF / BPF / XDP 和插件所需依赖，不以关闭透明代理能力换取体积。

精简从 S20P 复制来的 SATA / 原生 NVMe、USB 声卡、独立 EHCI / OHCI / UHCI、USB 网卡 / 手机共享 / 调制解调器、KSmbd、OpenVPN 回移模块、WireGuard 内核模块、bonding、Btrfs / FUSE 和重复分区工具等显式选包。保留 eMMC 维护工具、DWC3 USB3、automount / USB 存储及常用文件系统；由上游或插件依赖重新选中的包仍会保留。

这些精简大多减少 rootfs 中的模块和工具，不保证 gzip 内核一定能降到 7 MiB 内。两种压缩都保持上游 7168 KiB 内核限制和 eMMC 布局，超限即失败，不生成放宽限制的刷机包。赵云产物文件名与 `build-info.json` 标识实际压缩方式；日志附件提供 `kernel-config-*`、`kernel-sections-*`、`kernel-sizes.log`，便于对比内核配置与体积。

<details>
<summary>首次使用</summary>

| 项目 | 说明 |
| :--- | :--- |
| 默认设置 | 首次使用请修改管理与 Wi-Fi 密码；保留配置升级沿用原有设置 |
| 固件选择 | 核对机型、引导器与分区；S20P / 7DR7299 的 MTK 与 MT76 版不保证100%直接互刷 |
| 赵云 RE-CS-06 | 构建选项为 `jdcloud-re-cs-06`；仅适用于 RE-CS-06，非 RE-CS-08；默认使用 LZMA/FIT 内核，可选原始 gzip/FIT 对比，维持上游 7 MiB 内核区域与 eMMC 布局；刷入前确认引导器支持所选压缩格式 / FIT，勿将 sysupgrade 当作原厂首次刷入包 |

</details>
