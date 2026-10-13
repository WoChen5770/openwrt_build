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
| x86-64 | BBR · BIOS / UEFI / VMDK<br>PassWall（Xray-only）· daed / honk · EasyTier · AdGuard Home | [VIKINGYFY / owrt](https://github.com/VIKINGYFY/immortalwrt/tree/owrt) |

## 默认设置

硬件驱动按机型保留：BE12 Pro 显式集成 MT7992 无线固件及 MT7987/EN8811H 2.5G 网口组件；BE6500 保留 ath12k、PPE 与专用无线固件/校准数据。这两款使用单设备统一根文件系统，不集成 USB/自动挂载扩展。S20P-MT76 显式集成 MT7915/MT7986 无线和 MMC 驱动，保留原有 USB/存储支持。构建同时校验最终配置和固件包清单，防止必需驱动缺失或无关 USB 包回流。

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

Release 只提供固件镜像、带机型和日期的完整配置 `Config-*.txt`、包清单 `*.manifest`、`build-info.json` 和 `sha256sums`。x86 的 BIOS / UEFI / 虚拟磁盘等有效镜像格式继续保留。

`profiles.json`、上游 `*.buildinfo`、`config.seed` / `config.diff` 转到 Actions 的 logs 附件；重复说明 `release-notes.md` 只用于生成 Release 正文，不再单独上传。不再生成 `packages.tar.gz`（避免把固件下载页当作软件包源）。失败日志与固件附件保留 14 天，发布失败仍可通过 [Release recovery](https://github.com/WoChen5770/openwrt_build/actions/workflows/release.yml) 补发，无需重新编译。

清理仅针对本项目格式且带构建指纹的正式 Release 及识别出的编译缓存，未知附件、手工 Release、草稿、预发布不处理。1 天内的 Release 受到补发保护；最近一天创建或使用的缓存不删。有构建 / 补发任务未完成时跳过。每机型仅保留最新 1 个 Release，旧版本会一并删除其对应标签，固件无法从该 Release 恢复，请自行备份需要长期留存的版本。手动运行清理默认是预览模式。推送改动本身不会立即删除现有 Release。

## 目录结构

```text
.github/workflows/   构建、发布、补发、清理与检查入口
Config/              机型配置、共享片段和 JSON 设置
Scripts/             构建脚本、插件集成及配套资源
tests/               配置、构建和发布回归测试
```

配置统一在 `Config/` 修改，执行逻辑统一在 `Scripts/` 维护。`Scripts/Makefiles/`、`Scripts/Patches/`、`Scripts/Files/` 和 `Scripts/uci-defaults/` 都是构建正在使用的资源，不是历史备份。

## 自定义

<details>
<summary>配置文件与进阶选项</summary>

| 配置文件 | 用途 |
| :--- | :--- |
| [Config/GENERAL.txt](Config/GENERAL.txt) | 全机型公共配置：基础界面、组件、编译选项 |
| `Config/<机型>.txt` | 对应机型的硬件、插件选择，覆盖公共配置 |
| [Config/PASSWALL-XRAY-ONLY.txt](Config/PASSWALL-XRAY-ONLY.txt) | S20P 两版及 x86 共用的 PassWall 后端配置 |
| [Config/MT7986-AX6000.txt](Config/MT7986-AX6000.txt) | S20P MTK 专用厂商无线参数 |
| [Config/profiles.json](Config/profiles.json) | 机型与源码对应关系、片段合并顺序、硬件校验 |
| [Config/packages.json](Config/packages.json) | 插件源码与适用机型 |
| [Config/firmware-defaults.json](Config/firmware-defaults.json) | 网络、主题、时区 |
| [Config/build-settings.json](Config/build-settings.json) | 并发、自动构建、保留周期 |

| 进阶选项 | 说明 |
| :--- | :--- |
| `source_commit` | 单机型锁定上游完整 40 位 SHA |
| `packages` | 本次构建追加选包，优先级高于配置文件；只能填写 `CONFIG_PACKAGE_name=y/m/n` |
| `Config/PRIVATE.txt` | 可选的最后一层配置，应用到所选机型（`all` 时是全部机型） |
| `Scripts/PRIVATE.sh` | 可选 Bash 钩子，在锁定插件集成之后、默认设置 / QModem 清理之前执行；工作目录为 OpenWrt 源码根目录 |
| ccache | 上限 1 GiB；命中统计见 `logs/ccache.log` |
| host / 工具链缓存 | 不缓存 `staging_dir/target-*`；隔离机型、配置、feeds / 插件提交、构建规则及 runner / 编译器版本，命中后刷新 stamp 并清理 target 输出 |
| MTK 服务 | 不集成 Mesh；daed / honk 已集成，默认不启用 |

临时选包示例（可多行，也可用字面量 `\n` 分隔）：

```text
CONFIG_PACKAGE_luci-app-samba4=y
CONFIG_PACKAGE_samba4=y
```

合并顺序：`Config/GENERAL.txt` → 机型配置 → 该机型使用的共享片段 → `Config/PRIVATE.txt` → `packages`；具体片段顺序由 `Config/profiles.json` 的 `fragments` 列出，路径相对仓库根目录。硬性机型约束仍会校验，QModem 源码及链接始终移除；LuCI 集合只移除 attendedsysupgrade 依赖项，不删除其他依赖。临时 / PRIVATE 中请求的选项若被 `defconfig` 丢弃会报错，不会悄悄发布缺包固件。

PRIVATE 文件必须提交到默认分支，GitHub Actions 才能读取；“PRIVATE”只是沿用旧项目的命名，**不是秘密存储**，配置与构建记录会公开，切勿放入 Token、私钥或私密密码。钩子为可信代码，不执行临时输入；可用环境变量 `GITHUB_WORKSPACE`、`WRT_CONFIG`（旧机型名）、`WRT_TARGET`、`WRT_SUBTARGET`、`WRT_IP`、`WRT_MARK`、`WRT_DATE`、`WRT_BRANCH`、`WRT_INFO`。这是独立 Bash 进程，不提供旧 `Packages.sh` 的内部函数。

清理参数在 `Config/build-settings.json`：`releases_keep_per_profile`（每机型保留的 Release 数）、`release_recovery_days`（补发保护期）、`cache_keep_generations`、`cache_min_age_days`；`artifact_retention_days` 单独控制 Actions 附件保留期，与补发保护期互不影响。本地可执行 `GH_TOKEN=… GITHUB_REPOSITORY=owner/repo python3 Scripts/cleanup.py` 预览；只有添加 `--apply` 才删除。

</details>

<details>
<summary>首次使用</summary>

| 项目 | 说明 |
| :--- | :--- |
| 默认设置 | 首次使用请修改管理与 Wi-Fi 密码；保留配置升级沿用原有设置 |
| 固件选择 | 核对机型、引导器与分区；S20P MTK / MT76 不保证直接互刷 |

</details>
