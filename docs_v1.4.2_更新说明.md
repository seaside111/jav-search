# V1.4.2 更新说明（JavDB 专项优化）

本版本聚焦解决「勾选了 JavDB 却没有任何结果」的问题，打通 JavDB 抓取链路，并扩充信息量、规范多来源合并展示与下载推送体验。

## 背景

1.4.1 的 JavDB 刮削器只用了裸 `httpx` + 固定 UA，**既不过 Cloudflare 盾，也不带年龄确认 Cookie**。JavDB 风控较严，常返回 challenge 页 / 年龄门 / 地区封禁页（HTTP 200 但页面里没有片源卡片），解析为 0 条且错误被静默吞掉，于是 UI 上「悄悄消失」——看不到 JavDB，并非被同片合并隐藏（合并会把所有命中来源记进 `sources` 并显示成 `JavBus+JavDB`）。

## 一、JavDB 抓取链路打通

- **反爬增强**：自动携带 `over18`/`locale`/`theme` Cookie 绕过年龄门，补全完整浏览器请求头（`Sec-Fetch-*`、`sec-ch-ua` 等），失败自动重试并识别 CF 验证页。
- **可选 FlareSolverr**：配置 `FlareSolverr 地址` 后，JavDB 请求改走 FlareSolverr 取过盾后的 HTML（含 `cf_clearance`）。
  - Cookie 仅以 `name/value` 形式传入（带 `domain` 会令部分版本报 500）；带 Cookie 失败时自动重试一次不带 Cookie。
  - **代理账号密码自动拆分**：`http://user:pass@host:port` 会拆成 `url`+`username`+`password` 传给 FlareSolverr，解决其内置 Chrome 报 `ERR_NO_SUPPORTED_PROXIES`。
  - 新增开关 **「FlareSolverr 复用代理」**（默认开）：FlareSolverr 自带网络出口（如 WARP）时可关掉走直连。
- **手动 Cookie**：可在设置填入浏览器导出的 JavDB Cookie（如 `cf_clearance=...`），直连也能过盾。该字段读取时脱敏。

> 重要事实：**JavDB 因版权封禁「日本本土 IP」**。请使用非日本节点（美国/香港/台湾/新加坡等）的住宅 IP；机房 IP 也可能另被 CF 验证。

## 二、连通诊断（设置页「测试 JavDB 连通」）

新接口 `GET /api/javdb/test`，返回并在界面展示：
- 是否可达 / 是否命中 CF 盾 / HTTP 状态 / 解析条数 / 取页方式（flaresolverr / httpx）；
- **出口 IP 所在国家**（经同一代理查 ip-api，判断是否日本/机房 IP）；
- 0 条时**回显实际页面**的标题与片段，并判定页面类型：`年龄门 / 登录墙 / 地区封禁 / 人机验证 / 代理错误(ERR_NO_SUPPORTED_PROXIES)`，给出针对性建议；
- 出口 IP 探测失败时提示「代理本身不通/不稳」。

## 三、抓取信息扩充与合并规范

- JavDB 详情新增字段：**磁力链 `magnets`**（名称/大小/日期/高清/字幕标记）、**样品图 `samples`**、**评分人数 `score_count`**，演员/片商/发行/系列解析更稳。
- 合并去重（`_merge_lists`）：以高优先级来源（JavBus）为主条目保留封面/标题，用其它来源补全缺失字段（含 `score_count`/`actors`/`tags`/`samples`/`magnets`），命中来源全部记入 `sources`，展示为 `JavBus / JavDB`，规范不变。
- 合并条目记录各来源的详情页地址 `source_urls`，用于按需补抓非主来源的样品图/磁力。

### JavDB 样品图/磁力的显示
- 合并卡（JavBus+JavDB）主来源是 JavBus，**点开详情时**后台用 `source_urls['JavDB']` 补抓 JavDB 详情，仅并入样品图/磁力等独有字段并局部刷新该区块（不覆盖封面标题、不影响已翻译内容）。
- 新增开关 **「样品图预取」**（默认关）：开启后翻页预取时顺带提前抓好合并卡的 JavDB 样品图/磁力，点开即时显示，代价是 FlareSolverr 请求更多。

## 四、磁力下载推送

- JavDB 详情每条磁力新增 **推送下载(qB) / 迅雷 / 复制磁链** 按钮，与磁力信息同一行（靠右）。
- **推送到 qBittorrent 前自动给磁力补公共 tracker**：JavDB 多为「只有 hash 无 tracker」的裸磁力，qB 仅靠 DHT 在 NAT 下常卡在「下载元数据」0 数据；补 12 个稳定公共 tracker 后显著改善。
  - 若个别仍卡住：检查 qB 已启用 DHT/PeX/LSD、监听端口已转发、或该磁力确无活跃做种者。

## 五、刮削番号识别（按可靠度三级递进）

修复「推送下载后刮削番号标记不准」的问题。识别顺序：

1. **推送下载（Jackett / JavDB 磁力）**：推送时记录「番号 + 磁力 infohash」；刮削时查 qB API 找到该文件对应种子的 infohash，**精确反查记录的番号**（即详情页的字母+数字番号）。不依赖文件名，落地目录名再乱也能命中。
2. **推送兜底**：infohash 未命中时，按文件名/目录名与推送记录（番号/显示名）匹配反查。
3. **迅雷下载 / 手动复制到监控目录**（无推送标记）：直接按文件名正则识别番号——已剔除站点前缀（`hhd800.com@`）、`[标签]`、分集后缀（`-C`）等噪声，且不会把 `2024`/`1080p` 误判为番号。

附带：推送记录去重改为「仅按相同 infohash 去重」，同一影片推送多个磁力版本时，每个都能按各自 infohash 反查到正确番号。

## 六、浏览体验与性能

- **封面完整显示**：列表/首页卡片封面由 `object-fit: cover`（裁切）改为 `contain`，按布局等比显示完整封面。
- **单源超时**：搜索每个来源 25s、首页最新 40s，慢源（JavDB/FlareSolverr）超时即丢弃，不拖累其它来源合并返回。
- **FlareSolverr 限页**：JavDB 走 FlareSolverr 时列表只抓 1 页（约 28-40 条），避免多页累加超时；`maxTimeout` 40s。
- **详情预取窗口**：翻页后台串行预取后续 3 页详情，翻页更顺。
- **图片代理加固**：内存缓存 200→800，单次超时 20→12s，新增并发上限 12，避免一页多图把代理打满导致集体超时。

## 新增/变更配置项（`config/settings.json`）

```jsonc
{
  "javdb_flaresolverr_url": "",          // FlareSolverr 地址，留空走增强直连
  "javdb_flaresolverr_use_proxy": true,  // FlareSolverr 是否复用主代理；自带出口时关掉走直连
  "javdb_cookie": "",                    // 可选，浏览器导出的 JavDB Cookie（脱敏存储）
  "javdb_prefetch_extras": false         // 翻页时是否预取合并卡的 JavDB 样品图/磁力（默认关）
}
```

## 测试建议

1. 设置 →「JavDB 反爬」→ 填 FlareSolverr 地址（如有）→「测试 JavDB 连通」。
2. 按结论处理：被 CF/年龄门 → 配 FlareSolverr/Cookie；`ERR_NO_SUPPORTED_PROXIES` → 关「复用代理」或换无认证代理；地区封禁/日本 IP → 换非日本住宅节点。
3. 显示「✓ 可正常访问 + N 条」后搜索/看首页，确认出现 JavDB / `JavBus+JavDB` 卡片，点开详情看样品图与磁力，并测试推送到 qB。
