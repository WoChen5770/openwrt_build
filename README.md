# openwrt_build

五个独立配置的 ImmortalWrt 在线编译仓库。插件和基础配置迁自
[OpenWRT-CI](https://github.com/WoChen5770/OpenWRT-CI)，构建流程改为源码/feeds/插件提交锁定、单机型校验、独立产物与可选 Release 发布。

## 编译目标

| Actions profile | 上游 / 分支 | 实际设备 profile | 默认主要功能 |
| --- | --- | --- | --- |
| `xiaomi-be6500` | VIKINGYFY/immortalwrt / `owrt` | `qualcommbe/ipq53xx/xiaomi_be6500` | Mesh、DAWN，保留 ath12k/PPE 和校准数据 |
| `tenda-be12-pro` | 同上 | `mediatek/filogic/tenda_be12-pro` | Mesh、DAWN，保留设备无线和网络驱动 |
| `s20p-mt76` | 同上 | `mediatek/filogic/supergateway_s20p` | Mesh、daed/honk、PassWall/Xray、EasyTier、AdGuard Home |
| `x86-64` | 同上 | `x86/64/generic` | PassWall/Xray、daed/honk、EasyTier、AdGuard Home、BBR，BIOS/UEFI/VMDK 镜像；Virtio/PCIe 网卡、AHCI/NVMe、基础 USB/HID |
| `s20p-mtk` | chasey-dev/immortalwrt-mt798x-rebase / `25.12` | `mediatek/filogic/clx_s20p` | MTK 厂商无线、HNAT/WARP、TurboACC/eQoS、Mesh、daed/honk、PassWall/Xray、EasyTier、AdGuard Home |

所有机型预装中文 LuCI 和 shadcn，默认使用 shadcn 主题。
S20P 两版及 x86 的 PassWall 统一为 **Xray-only**；daed/honk 为独立插件，不受 PassWall 后端设置影响。honk 默认不启用，native API 保持关闭。
`make defconfig` 后再次检查驱动、重要插件及后端选择，发现与预期不符立即失败。

## 默认设置

集中修改 [firmware-defaults.json](firmware-defaults.json)：

- 管理地址：`192.168.123.1`，HTTP `80` / HTTPS `443`，不强制 HTTPS 跳转。
- 时区：`Asia/Shanghai`。
- Wi-Fi 名称：`ImmortalWrt`；密码：`12345678`；WPA2，默认打开（x86 无内置无线不产生热点）。
- 默认 shadcn；中文界面。
- `root` 默认不设置密码；首次登录务必设置。可填写 SHA256/SHA512 crypt 格式的 `root_password_hash`；不要填写明文，也不要把真实密码哈希提交到公开仓库。

主机名在 [profiles.json](profiles.json) 中按机型设置：

| 固件 | 默认主机名 |
| --- | --- |
| `xiaomi-be6500` | `XIAOMI-BE6500` |
| `tenda-be12-pro` | `TENDA-BE12-PRO` |
| `s20p-mt76` | `SUPERGATEWAY-S20P-MT76` |
| `s20p-mtk` | `SUPERGATEWAY-S20P-MTK` |
| `x86-64` | `X86` |

Wi-Fi/管理 IP 修改的是首次初始化生成器；保留配置升级时不会强制覆盖已有网络配置。主机名和默认主题由项目初始化脚本设置。
公开默认密码仅方便首次初始化，不能用于公网或不可信网络。

## 使用 GitHub Actions

1. 提交本仓库至 GitHub，在 Actions 中启用工作流。
2. 选择 **Build firmware → Run workflow**。
3. 选择 `profile`（建议首次逐机型验证），保留 `force=true`。
4. 如需 Release，勾选 `publish`；否则只保存 Actions Artifacts。
5. `source_commit` 可选：填写上游完整 40 位 SHA，仅允许单机型，不能用于 `all`。

`Repository checks` 只运行本地逻辑测试，不编译固件。`Build firmware` 才执行实际编译。
默认最多并行两个机型；每个机型同一时间只编译一个任务。不缓存跨版本 staging/toolchain，仅缓存下载与 ccache。
首次完整构建耗时与磁盘占用较大；Ubuntu 24.04 标准 runner 可能因大型插件依赖耗尽磁盘，需要根据实际日志调整配置或换更大 runner，当前不删除 runner 的预装目录来腾空间。

已开启每日北京时间 **04:15** 的源码更新检查（`build-settings.json` 中 `scheduled_builds=true`）。工作流文件提交到默认分支并启用 Actions 后才会生效，定时任务可能延迟，并非上游提交后立即触发。
定时运行比较各机型当前主源码提交与最近一次成功 Release：源码未变则跳过；有变化则编译对应机型，成功后自动发布 Release。首次无成功 Release 时构建全部机型。失败或未发布的构建不推进成功基线，下次继续尝试。
VIKINGYFY 的 `owrt` 更新会触发其四个机型；chasey 的 `25.12` 更新只触发 S20P MTK。仅 feeds、插件、Xray 或本仓库配置变化不触发定时固件构建，此时可手动强制编译。
构建指纹仍记录源码、feeds、插件、Xray 与仓库配置；手动 `force=false` 可按完整指纹跳过已有 Release。
不会自动删除历史 Release；Artifacts 默认保留 14 天。

## 产物与追溯

每个机型产出：设备匹配镜像、`sha256sums`、完整/差异/种子配置、`build-info.json`、同次构建的 `packages.tar.gz`。
构建计划记录源码、各 feeds、第三方包的实际提交。Xray 使用最新已发布官方二进制（包括预发布），以资产 ID 避免重传缓存，并验证官方 SHA256；资产缺失或未提供校验值会失败，不静默回退旧版。
失败任务也上传构建日志、配置与锁定计划。

`packages.tar.gz` 不是自动配置好的在线软件源。固件保留默认软件源，但滚动源未必与自编固件 ABI 匹配；内核模块应使用同次编译包，不能混装其他版本。

## S20P MTK 特别说明

“MTK 版/闭源版”指使用上游提供的厂商无线相关驱动栈，并非整个固件全部闭源。
按要求默认预装 Mesh、daed/honk；Mesh/daed/honk 服务默认不启用，MTK 版还关闭 Mesh 按键自动组网入口。
预装插件不等于 MTK 厂商无线支持 802.11s。未经过本机实测前不要把此版本视为可用无线 Mesh 固件。
HNAT/WARP 与透明代理可能冲突，启用 daed/honk 前先验证或关闭相关卸载；也不建议多个透明代理同时启用。

两版 S20P 的设备标识不同（`supergateway,s20p` 与 `clx,s20p`），不可默认直接互相 sysupgrade，更不能使用 `-F` 强刷。
首次安装需要针对现有引导器、分区和原固件确认方法；本仓库不改设备树、不重分区、不提供自动刷写脚本。

## 文件结构与维护

- `profiles.json`：源码、设备、配置组合与必须/禁止包。
- `Config/GENERAL.txt`：完整通用插件配置；各机型文件覆盖裁剪。
- `Config/SUPERGATEWAY-S20P-MT76.txt` / `Config/SUPERGATEWAY-S20P-MTK.txt`：两套独立的 S20P 驱动与插件配置。
- `configs/passwall-xray-only.config`：S20P 两版及 x86 共用的 PassWall 后端配置。
- `packages.json`：第三方插件源码集成策略。
- `scripts/build.py`：锁定、插件集成、默认设置、defconfig 校验与产物收集。
- `legacy/`：从旧仓库迁入的 Mesh/honk 适配、UCI 初始化与 Makefile 模板，保留原许可证。
- `.github/workflows/`：编译入口、可复用编译/发布流程、静态测试。

本地无需额外依赖即可运行逻辑测试：

```sh
python3 -m unittest discover -s tests -v
```

`python3 scripts/build.py plan --event schedule` 是真实更新检查，需要网络、GitHub CLI 和 `GITHUB_REPOSITORY` 环境变量，不属于离线测试。

真正编译需要 Linux。单元测试/脚本语法通过不代表固件编译通过，更不代表刷机或 Mesh/代理功能已实测。
