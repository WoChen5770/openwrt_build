<div align="center">

# ImmortalWrt Build

五种固件 · 自动构建 · 独立发布

[![Build](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml/badge.svg)](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/WoChen5770/openwrt_build?style=flat&color=2563eb)](https://github.com/WoChen5770/openwrt_build/releases)

[下载固件](https://github.com/WoChen5770/openwrt_build/releases) · [在线构建](https://github.com/WoChen5770/openwrt_build/actions/workflows/build.yml) · [自定义配置](#自定义)

</div>

## 选择机型

| 机型 | 构建目标 | 主机名 | 特性 |
| :--- | :--- | :--- | :--- |
| Xiaomi BE6500 | `xiaomi-be6500` | `XIAOMI-BE6500` | ath12k · PPE · Mesh / DAWN |
| Tenda BE12 Pro | `tenda-be12-pro` | `TENDA-BE12-PRO` | MT76 · Mesh / DAWN |
| S20P · MT76 | `s20p-mt76` | `S20P` | 开源无线 · Mesh |
| S20P · MTK | `s20p-mtk` | `S20P` | 厂商无线 · HNAT / WARP · TurboACC / eQoS · Mesh |
| x86-64 | `x86-64` | `X86` | BBR · BIOS / UEFI / VMDK |

| 项目 | 适用机型 | 内容 |
| :--- | :--- | :--- |
| 主源码 | 除 S20P MTK 外的机型 | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| 主源码 | S20P MTK | [chasey-dev / 25.12](https://github.com/chasey-dev/immortalwrt-mt798x-rebase/tree/25.12) |
| 集成插件 | S20P 两版、x86 | PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home |

## 默认设置

| 项目 | 设置 |
| :--- | :--- |
| 管理入口 | `192.168.123.1` · HTTP / HTTPS |
| 管理账号 | `root` · 默认空密码 |
| Wi-Fi | `ImmortalWrt` · 密码 `12345678` · WPA2 |
| 界面 | shadcn · 简体中文 |
| 时区 | Asia/Shanghai |
| 设置生效 | 保留配置升级沿用原有网络设置；x86 无内置无线时不生成热点 |
| 首次使用 | 设置管理密码、修改 Wi-Fi 密码；勿将管理界面暴露到公网 |

## 构建与发布

| 项目 | 说明 |
| :--- | :--- |
| 手动构建 | Actions → Build firmware → Run workflow |
| 机型选择 | 指定单个目标，或选择 `all` 并行构建五种固件 |
| 默认行为 | `force=true` 强制构建 · `publish=true` 发布 Release；关闭 `publish` 仅保存 Artifacts |
| 自动构建 | 每日北京时间 **04:15** 检查主源码，有更新才构建 |
| 手动更新 | 仅插件、feeds 或本仓库配置变化时，需手动构建 |
| 构建缓存 | 下载缓存 + ccache，按机型隔离 |
| 发布内容 | 固件、校验文件、完整配置与构建记录 |
| 保留周期 | Artifacts / 日志保留 14 天 |
| 发布补偿 | 失败后自动补发，或运行 **Recover firmware releases**；无需重编 |

## 自定义

<details>
<summary>配置文件与进阶选项</summary>

| 配置文件 | 用途 |
| :--- | :--- |
| [firmware-defaults.json](firmware-defaults.json) | 网络、主题、时区 |
| [profiles.json](profiles.json) / [Config/](Config/) | 机型、主机名、插件 |
| [packages.json](packages.json) | 插件源码 |
| [build-settings.json](build-settings.json) | 并发、自动构建、保留周期 |

| 进阶选项 | 说明 |
| :--- | :--- |
| `source_commit` | 单机型锁定上游完整 40 位 SHA |
| ccache | 上限 1 GiB；命中统计见 `logs/ccache.log` |
| `packages.tar.gz` | 同次构建的包归档，非在线软件源；内核模块须匹配 ABI |

</details>

## 刷写须知

| 项目 | 注意事项 |
| :--- | :--- |
| 刷写检查 | 核对机型、引导器与分区，勿强刷；构建成功不代表实机验证通过 |
| S20P 互刷 | MTK / MT76 不保证直接互刷 |
| MTK 旧版 | `s20p-mtk-r38073396755-1` 缺少无线配置组件，请使用修复后重新构建的版本 |
| MTK 无线 | “闭源版”仅指厂商无线栈；Mesh / daed / honk 默认关闭，802.11s 尚未验证 |
| MTK 加速 | HNAT / WARP 与透明代理的兼容性需实机验证 |
