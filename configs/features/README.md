# 功能片段

当前完整功能配置已从旧仓库迁入 `Config/`，由 `profiles.json` 显式组合。

PassWall 后端已统一到 `configs/passwall-xray-only.config`，由 S20P 两版及 x86 共用。
后续可以将 Mesh、AdGuard Home、EasyTier、存储等拆成独立
`.config` 片段，在 `profiles.json` 的 `fragments` 中显式启用，并同步维护
`required` 校验。第三方软件需增加固定提交的源码适配，不能仅填一个不存在的包名。

S20P MTK 版不集成 luci-app-mesh：厂商无线驱动未上报标准 802.11s mesh point 能力。
daed/honk 和 eBPF 依赖仍预装，服务默认不启用；HNAT/WARP 也可能与透明代理冲突。
