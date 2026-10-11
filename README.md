<div align="center">

# ImmortalWrt Build

一套流程，五种固件。源码更新后自动构建，按机型独立发布。

[![Build](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml/badge.svg)](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/WoChen5770/openwrt_build?style=flat&color=2563eb)](https://github.com/WoChen5770/openwrt_build/releases)

[下载固件](https://github.com/WoChen5770/openwrt_build/releases) · [在线构建](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml) · [自定义配置](#自定义)

</div>

## 选择机型

| 机型 | 构建目标 | 特性 |
| :--- | :--- | :--- |
| Xiaomi BE6500 | `xiaomi-be6500` | ath12k · PPE · Mesh / DAWN |
| Tenda BE12 Pro | `tenda-be12-pro` | MT76 · Mesh / DAWN |
| S20P · MT76 | `s20p-mt76` | 开源无线 · Mesh |
| S20P · MTK | `s20p-mtk` | 厂商无线 · HNAT / WARP · TurboACC / eQoS · Mesh |
| x86-64 | `x86-64` | BBR · BIOS / UEFI / VMDK |

S20P 两版与 x86 集成 **PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home**。

源码：[VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt)；S20P MTK 使用 [chasey-dev / 25.12](https://github.com/chasey-dev/immortalwrt-mt798x-rebase/tree/25.12)。

> S20P MTK 旧版 `s20p-mtk-r38073396755-1` 缺少无线配置组件，请使用修复后重新构建的版本。

## 默认设置

| 项目 | 设置 |
| :--- | :--- |
| 管理入口 | `192.168.123.1` · HTTP / HTTPS · `root` |
| Wi-Fi | `ImmortalWrt` · 密码 `12345678` · WPA2 |
| 界面 | shadcn · 简体中文 |
| 时区 | Asia/Shanghai |

主机名：`XIAOMI-BE6500` / `TENDA-BE12-PRO` / `S20P` / `X86`。x86 无内置无线时不生成热点；保留配置升级沿用原有网络设置。

> `root` 默认空密码。首次登录请设置管理密码、修改 Wi-Fi 密码，勿将管理界面暴露到公网。

## 构建与发布

**Actions → Build firmware → Run workflow**，选择机型或 `all` 即可。默认强制构建并发布 Release；关闭 `publish` 则仅保存 Artifacts。

五目标并行 · 每日北京时间 **04:15** 检查主源码更新 · 下载缓存 + ccache。仅插件、feeds 或本仓库配置变化时需手动构建。

Release 附带校验文件、完整配置与构建记录；Artifacts / 日志保留 14 天。发布失败可自动补发，也可运行 **Recover firmware releases**，无需重编。

## 自定义

<details>
<summary>配置文件与进阶选项</summary>

- [firmware-defaults.json](firmware-defaults.json)：网络、主题与时区。
- [profiles.json](profiles.json) / [Config/](Config/)：机型、主机名与插件。
- [packages.json](packages.json)：插件源码。
- [build-settings.json](build-settings.json)：并发、自动构建与保留周期。

`source_commit` 可锁定单机型的上游完整 SHA。缓存按机型隔离，ccache 上限 1 GiB；命中统计见 `logs/ccache.log`。`packages.tar.gz` 为同次构建的包归档，不是在线软件源，内核模块须匹配 ABI。

</details>

> 刷写前核对机型、引导器与分区，勿强刷；S20P MTK / MT76 不保证直接互刷。MTK 的“闭源版”仅指厂商无线栈：Mesh / daed / honk 默认关闭，802.11s 尚未验证，HNAT / WARP 与透明代理需验证兼容性。构建成功不代表实机验证通过。
