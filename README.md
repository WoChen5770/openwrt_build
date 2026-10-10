<div align="center">

# ImmortalWrt · Build

**多机型固件 · 并行构建 · 自动发布**

[![Build](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml/badge.svg)](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml)
[![Checks](https://github.com/WoChen5770/openwrt_build/actions/workflows/check.yml/badge.svg)](https://github.com/WoChen5770/openwrt_build/actions/workflows/check.yml)
[![Release](https://img.shields.io/github/v/release/WoChen5770/openwrt_build?style=flat&color=2563eb)](https://github.com/WoChen5770/openwrt_build/releases)

[下载固件](https://github.com/WoChen5770/openwrt_build/releases) · [在线编译](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml) · [机型配置](profiles.json)

</div>


## 机型与功能

| 编译目标 | 默认主要功能 |
| :--- | :--- |
| **Xiaomi BE6500** · `xiaomi-be6500` | Mesh / DAWN · ath12k / PPE |
| **Tenda BE12 Pro** · `tenda-be12-pro` | Mesh / DAWN |
| **S20P · MT76** · `s20p-mt76` | Mesh · daed / honk · PassWall · EasyTier · AdGuard Home |
| **S20P · MTK** · `s20p-mtk` | 厂商无线 / HNAT / WARP · TurboACC / eQoS · Mesh · daed / honk · PassWall · EasyTier · AdGuard Home |
| **x86-64** · `x86-64` | daed / honk · PassWall · EasyTier · AdGuard Home · BBR · BIOS / UEFI / VMDK |

BE6500、BE12 Pro、S20P MT76 和 x86 使用 [VIKINGYFY / immortalwrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) 的 `owrt` 分支；S20P MTK 使用 [chasey-dev / immortalwrt-mt798x-rebase](https://github.com/chasey-dev/immortalwrt-mt798x-rebase/tree/25.12) 的 `25.12` 分支。所有 PassWall 均为 **Xray-only**；honk 预装但默认不启用。

## 默认设置

| 项目 | 设置 |
| :--- | :--- |
| 管理入口 | `192.168.123.1` · HTTP / HTTPS |
| Wi-Fi | `ImmortalWrt` · `12345678` · WPA2 |
| 界面 | shadcn · 简体中文 |
| 时区 | Asia/Shanghai |

主机名按机型命名：`XIAOMI-BE6500`、`TENDA-BE12-PRO`、`SUPERGATEWAY-S20P-MT76`、`SUPERGATEWAY-S20P-MTK`、`X86`。x86 无内置无线时不生成热点；保留配置升级不会覆盖已有 Wi-Fi / 管理 IP。

> `root` 默认空密码。首次登录请设置管理密码并修改 Wi-Fi 密码，勿直接暴露到公网或不可信网络。

## 在线编译

1. 打开 **Actions → Build firmware → Run workflow**。
2. 选择单个 `profile` 或 `all`；`force=true` 可强制重编。
3. `publish` 默认开启：各机型成功后独立发布 Release；关闭则仅保存 Artifacts。

支持 **五目标并行**，实际并发受 GitHub 额度限制。每日北京时间 **04:15** 检查主源码更新，仅构建有变化的机型；首次无成功 Release 时构建全部。仅插件、feeds 或本仓库配置变化时，请手动触发。`source_commit` 可锁定上游完整 40 位 SHA，仅限单目标。

构建自动复用 **下载缓存 + ccache 编译缓存**，按机型隔离；ccache 上限为每机型 1 GiB，首次构建填充后供后续复用，命中统计保存在 `logs/ccache.log`。不跨版本缓存 staging / toolchain，实际提速取决于缓存命中率。

Release 包含固件、`sha256sums`、配置、`build-info.json` 与 `packages.tar.gz`；Actions 产物保留 **14 天**。失败也保留诊断日志，不自动删除历史 Release。`packages.tar.gz` 不是在线软件源，内核模块须使用同次构建包，避免 ABI 不匹配。

发布失败会自动复用成功产物补发，也可通过 **Recover firmware releases** 填写原任务 ID 手动补发，无需重新编译。Release 标签用于发布索引，实际构建提交记录在 `build-info.json` 与发布说明中。

<details>
<summary><strong>配置入口与刷写须知</strong></summary>

- [firmware-defaults.json](firmware-defaults.json)：管理 IP、Wi-Fi、主题与时区。
- [profiles.json](profiles.json) / [Config/](Config/)：机型、主机名、驱动与插件组合。
- [packages.json](packages.json)：插件源码；[build-settings.json](build-settings.json)：并发、自动构建与产物保留。

S20P MTK 的“闭源版”指厂商无线驱动栈，并非整个固件闭源。Mesh / daed / honk 服务默认关闭；预装 Mesh 不等于厂商无线支持 802.11s，尚需实机验证。HNAT / WARP 与透明代理可能冲突，启用代理前应验证或关闭相关卸载。

S20P 两版设备标识不同，不可默认互相 sysupgrade，更不要使用 `-F` 强刷。首次安装需确认引导器、分区与原固件；本仓库不重分区、不提供自动刷写脚本。构建成功不代表刷写、Mesh 或代理功能已实测。

</details>
