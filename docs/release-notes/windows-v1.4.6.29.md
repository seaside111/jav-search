# Windows v1.4.6.29

此版本将 Docker v1.4.6.28 的 JavBus 详情扩展抓取同步到 Windows 桌面版，同时保持两个发布通道完全独立。

- 详情页样品图和投稿磁力改由 JavBus 获取，不再使用 JavDB 作为详情扩展来源。
- JavBus 无样品图时按精确番号调用 JAV321 一次兜底，仍无结果即停止。
- JAV321 只用于详情样品图兜底，不进入首页、普通搜索或可配置来源列表。
- 修复 JAV321 将 DMM 大封面误判为样品图，并自动刷新旧版 JavBus 详情缓存。
- 使用 `SUN-067` 验证 JavBus 返回 20 张样品图和 10 条磁力，JAV321 兜底返回 20 张有效样品图。
- 保留 Windows 本机下载器、独立更新器及安装包 SHA256 验证；本发布使用 `windows-v1.4.6.29` Pre-release 标签，不影响 Docker `latest`。
