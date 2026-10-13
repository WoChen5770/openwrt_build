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
| Xiaomi BE6500 | ath12k · PPE · Mesh / DAWN | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| Tenda BE12 Pro | MT76 · Mesh / DAWN | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| S20P · MT76 | 开源无线 · Mesh<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| S20P · MTK | 厂商无线 · HNAT / WARP · TurboACC / eQoS<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [chasey-dev / 25.12](https://github.com/chasey-dev/immortalwrt-mt798x-rebase/tree/25.12) |
| TL-7DR7299 v1 · MT76 | 开源无线 · Mesh / DAWN<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |
| TL-7DR7299 v1 · MTK | 闭源 Wi-Fi 7 · HNAT / WARP · TurboACC / eQoS<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [padavanonly / 6.6-mtwifi](https://github.com/padavanonly/immortalwrt-mt798x-6.6/tree/mt798x-mt799x-6.6-mtwifi) |
| x86-64 | BBR · BIOS / UEFI / VMDK<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |

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
| 缓存加速 | 下载缓存 · ccache · 兼容配置的工具链 / host 缓存 |
| 固件发布 | 构建成功后默认发布 Release |
| 版本标识 | LuCI 固件版本显示仓库所有者、北京时间（编译开始）；镜像文件名带机型、同一时间和运行编号 |
| 升级页面 | 恢复出厂 / 不保留配置升级后重连默认管理 IP；保留配置升级仍沿用当前地址 |
| 自动清理 | 每日北京时间 **02:30**；每机型只保留最新 1 个 Release，每类缓存保留至少 2 代 |

Release 提供固件、完整配置、包清单及校验文件。Actions 日志与附件保留 14 天；发布失败可通过 [Release recovery](https://github.com/WoChen5770/openwrt_build/actions/workflows/release.yml) 补发。

## 目录结构

```text
.github/workflows/   构建、发布、补发、清理与检查入口
Config/              独立机型配置和 TOML 设置
Scripts/             构建脚本、插件集成及配套资源
tests/               配置、构建和发布回归测试
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
| Python | 构建脚本需要 3.11+，Actions 已满足 |
| `packages` | 本次构建追加选包，优先级高于配置文件；只能填写 `CONFIG_PACKAGE_name=y/m/n` |
| `Config/PRIVATE.txt` | 可选的最后一层配置，应用到所选机型（`all` 时是全部机型） |
| `Scripts/PRIVATE.sh` | 可选 Bash 钩子，在锁定插件集成之后、默认设置 / QModem 清理之前执行；工作目录为 OpenWrt 源码根目录 |
| ccache | 上限 1 GiB；命中统计见 `logs/ccache.log` |
| host / 工具链缓存 | 按机型与构建配置隔离，不缓存 target 产物 |
| MTK 服务 | 不集成 Mesh；daed / honk 已集成，默认不启用 |

临时选包示例（可多行，也可用字面量 `\n` 分隔）：

```text
CONFIG_PACKAGE_luci-app-samba4=y
CONFIG_PACKAGE_samba4=y
```

覆盖顺序：机型配置 → `Config/PRIVATE.txt` → `packages`；选包丢失或违反机型约束会终止构建。

PRIVATE 文件需提交到构建分支；配置与构建记录会公开，勿放入 Token、私钥或私密密码。

清理参数见 `Config/build-settings.toml`，手动清理默认仅预览；需要长期保留的固件请自行备份。

</details>

<details>
<summary>首次使用</summary>

| 项目 | 说明 |
| :--- | :--- |
| 默认设置 | 首次使用请修改管理与 Wi-Fi 密码；保留配置升级沿用原有设置 |
| 固件选择 | 核对机型、引导器与分区；S20P / 7DR7299 的 MTK 与 MT76 版不保证直接互刷 |

</details>
