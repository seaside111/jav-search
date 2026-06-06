# V1.4.3 更新说明 —— FC2 数据源 + 首页体验增强

> **本版为 Beta（`v1.4.3-beta`）**。Docker 镜像走独立 `:beta` 标签，不影响正式版 `:latest` 用户。

## 一句话

① 新增 **FC2-PPV 专用数据源**（fc2ppvdb.com），把 FC2 素人/无码片源纳入检索与刮削；
② 首页顶部新增**版本号 + 更新检测**；③ 首页最新片源改为**各来源并行、边抓边显示**，
并提供**数据源切换标签**快速筛选；④ **FlareSolverr 地址智能适配** + **一键 docker 安装包（内置 FlareSolverr）**。

---

## FlareSolverr 地址智能适配（本版新增，三源共享）

JavDB / FC2 / MissAV 统一走 `scrapers/_fsgate.py` 的共享取页层，针对各种 NAS/服务器安装习惯自动适配 FlareSolverr 地址，**填错也尽量能连上**：

- **写法全兼容**：缺 `http://` 自动补、不填端口默认 `8191`、带 `/v1` 或末尾斜杠自动清理。
- **Docker `localhost` 自动改写**：容器内填 `localhost`/`127.0.0.1` 指向的是本服务自己（必然连不上），
  自动按命中概率依次尝试 `host.docker.internal` → 同网络容器名 `flaresolverr` → 网桥网关 `172.17.0.1`。
  局域网 IP / 域名不会被改写，非 Docker 部署也不扩展，避免误伤正常配置。
- **快失败 + 缓存**：连接超时压到 6s（死地址不再卡 70s），连上的地址会被缓存，后续请求直接命中。
- **诊断更直白**：连通诊断回显「实际连通地址」，自动适配时提示建议改成的地址；全连不上时列出已尝试的全部地址 + Docker 正确填法。
- **环境变量预置**：`JAVDB_FLARESOLVERR_URL` / `FC2_FLARESOLVERR_URL` 在设置页留空时生效（用户填了以设置页为准），供一键包零配置预置。

> ⚠️ 智能适配解决的是「**连不上 FlareSolverr**」。若部署在机房 VPS（数据中心 IP），JavDB 仍可能按 IP 信誉**硬封 403**——
> 这需要在「设置 → 主代理」填一个【**非日本**】住宅代理，让 FlareSolverr 透过它出网（保持「FlareSolverr 复用主代理」开启）。

## 一键 docker 安装包（内置 FlareSolverr）

- `docker-compose.hub.yml`：**零构建**，拉取 `ghcr.io/seaside111/jav-search:beta` + 内置 FlareSolverr，同网络、env 已预置好地址，开箱即用：
  ```bash
  docker compose -f docker-compose.hub.yml up -d
  ```
- `docker-compose.flaresolverr.yml`：拿到**完整源码包**后从本地 Dockerfile 构建 + 内置 FlareSolverr，不依赖 GHCR：
  ```bash
  docker compose -f docker-compose.flaresolverr.yml up -d --build
  ```
- `.github/workflows/docker-publish.yml`：推送 `V1.4.3-beta` 这类 tag 时自动构建多架构镜像；beta tag 打 `:beta`，正式 tag 打 `:latest`，互不干扰。

---

## 首页体验增强（本版新增）

### 1. 版本号与更新检测

- 顶部搜索框右侧显示当前版本 `v1.4.3`。
- 后端新增 `GET /api/version`，**由后端代理访问 GitHub** 取最新 release 并**缓存 1 小时**
  （可走配置代理出网，群晖内网也能用，避免浏览器跨域 / 被 GitHub 限流）。
- 检测到更高版本时，版本号旁出现**红点脉冲**提醒，点击跳转 GitHub release 页。
- 比较为语义化比较，自动忽略 `V` 前缀（`V1.4.3` ↔ `1.4.3`）。

### 2. 最新片源「边抓边显示」

- 旧版 `/api/latest` 一次性等所有源合并完才显示；本版前端对**每个来源并行请求**
  `GET /api/latest?source=xxx`，**谁先回来谁先显示**，慢源（如走 FlareSolverr 的 JavDB/FC2）
  随后按番号去重补入，不再被慢源拖住整页。
- 状态栏实时显示进度（如「最新片源加载中（1/3） · 已到 JavBus」）。
- 合并按番号去重，多源命中的卡片合并来源标记，且**不重排**以保证详情按需补全的索引稳定。

### 3. 数据源切换标签

- 列表上方出现数据源筛选条（如 `全部 / JavBus / JavDB / FC2`，各带条数），点击只看该来源。
- 合并卡（如 JavBus+JavDB 同番号）在所属的每个来源下都会出现。
- 筛选条在**首页最新与搜索结果**下均生效（仅单一来源时自动隐藏）。

---

## MissAV 补全 FC2 封面/标题（本版新增）

fc2ppvdb 对**已下架**的 FC2-PPV 常只剩番号骨架（无封面/标题）。本版接入 **MissAV** 作为
FC2 的补全源——它对 FC2 覆盖很好，连下架条目都保留**完整标题 + 高清封面**（封面走
`fourhoi.com` CDN）。

- **列表卡封面**：fc2ppvdb 缺封面时，直接用 MissAV 的**确定性封面 URL**
  （`https://fourhoi.com/fc2-ppv-{番号}/cover-n.jpg`）兜底——**零额外请求**，404 由前端图片代理
  优雅降级为无封面。
- **详情标题/封面**：打开详情时**并发**抓 fc2ppvdb 与 MissAV，用 MissAV 补 fc2ppvdb 缺失的
  标题/封面（几乎不增加延迟）。补全后来源标记为 `FC2 + MissAV`。
- **样品图**：MissAV 无传统图廊，但播放器进度条带一组**逐帧截图**
  （`https://{cdn}/{uuid}/seek/_N.jpg`，CDN 如 nineyu.com，约 10+ 张）——这些就是可用的样品图，
  已并入 FC2 详情的「样品图」区块（与 JavDB 样品图同款展示）。另带 `preview.mp4` 预览视频
  字段（确定性 URL，前端暂未渲染，留作后续动态预览）。
- 这些 MissAV 系 CDN（fourhoi/nineyu/surrit/sixyik）**有防盗链**，需带 `Referer: missav` 域名；
  已在图片代理 `/api/img` 增加对应 Referer 规则，前端无感。
- 只取 MissAV 的**标题 + 封面 + 样品图**（高可靠）；不抓其女优/体裁（其导航里混有
  「女優ランキング」等噪声，难以可靠区分）。FC2 素人片本就少有结构化女优/体裁。
- 设置页「FC2 数据源」组新增「MissAV 补全」开关（默认开）与镜像填写框（域名常变，可填多个
  逗号分隔做兜底）。

> 关于 Jackett 里看到的标题：那是**种子发布名**（sukebei 上传者手填），非结构化元数据，
> 所以带 `+++`、特典等噪声。本版的标题/封面来自 MissAV，干净规整。

## FlareSolverr 并发治理（修复批量详情超时）

引入 FC2（同走 FlareSolverr）后，翻页时**批量补全整页详情**会把 6 个并发请求同时砸向
FlareSolverr。而 **FlareSolverr 是单浏览器实例、不支持并发**，请求互相排队挤到**集体超时**
（日志里成片的 `[enrich] javdb/fc2 ... timeout`）。注意：这不是 IP 失效——单请求的连通测试/
点开单条详情仍正常。两处修复：

1. **后端串行闸**（`scrapers/__init__.py`）：走 FlareSolverr 的来源（JavDB/FC2）改走进程级
   `Semaphore(1)` **全局串行**，一个一个来；直连来源（JavBus/AVSOX/AVMOO）仍并发。
2. **前端慢源不批量预取**（`index.html`）：配了 FlareSolverr 时，JavDB/FC2 详情**不参与翻页
   批量预取**，改为**点开详情时单条按需补全**（`ensurePageDetails` 跳过慢源，`openDetail →
   ensureItemDetail` 兜底）。合并卡（主源 JavBus）不算慢源，照常预取，其 JavDB 样品图/磁力仍
   点开时按需补抓——既不压垮 FlareSolverr，又不丢信息。

> 经验：FlareSolverr 单实例吞吐有限，慢源详情天然只能「逐条按需」。若要批量更快，需多开
> FlareSolverr 实例或换更快的过盾方案。

3. **全局取页层串行闸**（`scrapers/_fsgate.py`）：上面的串行闸只管 `enrich`（详情批量），
   而**搜索/诊断/最新**走的是另外的路径没保护——后台刮削搜 JavDB 占用 FlareSolverr 时，前台点
   「JavDB 连通测试」会排队挂起直到 `ReadTimeout`（典型误判为「IP 失效」，实则是撞车）。
   修复：新建进程级 `GATE = Semaphore(1)`，JavDB/FC2/MissAV 的 FlareSolverr 取页（POST）**全部
   通过这一把闸全局串行**，一次只发一个请求。诊断会「排队等待」而非「撞车超时」。
   注意：这只能消除撞车；**机房 IP（如 Cogent）过 JavDB 盾本就难，根治需换非日本住宅 IP**。

## 无码番号识别修复（library.py）

刮削把 `hhd800.com@060226_01-10MU` 误识别成 `HHD-800`。双缺陷：① 真实番号 `060226_01`
（10musume `MMDDYY_NN`）不在番号正则里，匹配失败；② 失败后回退去匹配**未去广告的原始名**，
抓到广告域名 `hhd800` → `HHD-800`。修复：

- **回退也剥广告域名**：`_code_from_name` 回退时用 `_SITE_NOISE` 清理过的串，广告域名不再被当番号。
- **增加无码番号正则**：`\d{6}[-_]\d{2,4}`（10musume/1pondo/Caribbean 的 `060226_01`/`060226-001`/
  `060226_001`）、`[A-Z]{3,10}-\d{3,5}-\d{2,4}`（heydouga-4017-001，放在普通字母番号前防截断）。
- 有码番号（ABP/SSIS/390JAC/FC2 等）不受影响；广告名不再被瞎猜成番号。

> 注意：识别正确 ≠ 能刮到元数据。刮削目前只搜 javbus/javdb，10musume 等无码厂牌不在其上，
> NFO 元数据可能仍为空——需后续给刮削接入无码数据源才能解决。

## 顺带验证：JavDB 样品图/磁力功能完好

本版核对确认：后端 `javdb.py` 与 1.4.2 **字节级完全一致**，前端 JavDB 样品图/磁力的
按需补抓与详情展示函数（`detailNeedsJavdbExtra` / `renderDetailExtra` / `ensureJavdbExtras`）
**一个都没删、行为不变**；新增的数据源过滤采用「主索引」视图，不影响按索引补全详情。
另外把 `enrich` 的单条超时放宽规则从「仅 JavDB」扩展到「JavDB + FC2」（两者都走 FlareSolverr，
需要更长超时），让 FC2 关键词搜索的详情补全也不会过早超时。

## FC2-PPV 数据源

新增 **FC2-PPV 专用数据源**（fc2ppvdb.com），把 FC2 素人/无码片源纳入检索与刮削，
番号、标题、封面、女优、卖家、贩卖日、收录时间、标签一并抓取。

---

## 为什么单独做 FC2

FC2-PPV 是独立番号体系（如 `FC2-PPV-1234567`），此前只有 JavDB 偶尔顺带收录少量，
搜全率和字段完整度都很差。本版接入 **fc2ppvdb.com**——FC2-PPV 专用数据库，字段最规整。

fc2ppvdb 与 JavBus 系（avsox/avmoo 复用 `_javbus_base`）页面模板完全不同（Laravel + Tailwind），
因此单独写了 [`backend/scrapers/fc2.py`](backend/scrapers/fc2.py)，未复用 JavBus 模板。

---

## 关键点：必须配 FlareSolverr

fc2ppvdb **强制 Cloudflare Turnstile 人机验证**，直连只能拿到验证页（实测直连仅返回
Turnstile 挑战页 + 登录表单）。因此 FC2 取页复用了 JavDB 那套
「FlareSolverr 优先 + 增强 httpx 兜底」的策略：

- **FC2 的 FlareSolverr 地址留空时，自动复用 JavDB 已填的 `javdb_flaresolverr_url`**，
  已经为 JavDB 配过 FlareSolverr 的用户无需重复填写。
- 也可在「设置 → FC2 数据源（1.4.3）」单独指定 FC2 专用 FlareSolverr 地址、是否复用主代理、
  可选手动 Cookie。

---

## 番号识别

以下写法都会被识别为「番号检索」，直接命中 `fc2ppvdb.com/articles/{番号}` 详情页（最稳最快）：

- `FC2-PPV-1234567`
- `FC2PPV1234567`
- `FC2-1234567`
- 纯数字 `1234567`（6–7 位）

关键词 / 女优搜索走站内搜索 `fc2ppvdb.com/search?stext=...`。

---

## 下载链路说明（重要）

fc2ppvdb **不提供磁力链**。检索（元数据）与抓取（磁力/种子）是两条链路：

- 元数据：本版的 FC2 数据源负责。
- 下载：仍走你现有的 **Jackett / sukebei**，用番号 `FC2-PPV-xxxxxxx` 检索后推送 qBittorrent。

卡片上的「Jackett 搜索 / 推送下载」按钮对 FC2 同样可用，番号已统一规范成 `FC2-PPV-xxxxxxx`
（sukebei 最通用的写法）。

---

## 使用步骤

1. 设置 → 常规 → 数据源，勾选 **FC2**。
2. 设置 → 「FC2 数据源（1.4.3）」：FlareSolverr 地址留空即复用 JavDB 的；如未配过，填一个
   FlareSolverr 地址（如 `http://192.168.1.100:8191`）。
3. 点「测试 FC2 连通」确认可达、未被 Turnstile 拦截。
4. 搜索框输入 `FC2-PPV-1234567` 或女优名 → 出现 FC2 结果卡片。
5. （可选）首页最新片源里勾上 FC2；FC2 走 FlareSolverr 单页较慢，数量建议 30 条左右。

---

## 改动清单

| 文件 | 改动 |
|------|------|
| `backend/scrapers/fc2.py` | **新增**：FC2 刮削器（列表/详情/最新/连通诊断，FlareSolverr 取页） |
| `backend/scrapers/__init__.py` | 注册 `fc2` 数据源、合并优先级；`detect_search_mode` 增加 FC2 番号识别 |
| `backend/config_manager.py` | 新增 `fc2_flaresolverr_url` / `fc2_flaresolverr_use_proxy` / `fc2_cookie`；`latest_limits` 增加 `fc2` |
| `backend/main.py` | 版本号 1.4.3；配置模型与脱敏增加 FC2 字段；新增 `GET /api/fc2/test` 诊断接口；新增 `GET /api/version`（版本检测，带缓存/代理）；`GET /api/latest` 支持 `?source=` 单源抓取 |
| `frontend/index.html` | 数据源勾选增加 FC2；新增「FC2 数据源」设置组；首页最新数量增加 FC2；**顶部版本号 + 更新红点**；**最新片源各源并行边抓边显示**；**数据源切换标签**；读写配置联动 |
| `README.md` | 数据源说明、项目结构、API 表、更新日志 |

> 实现要点：前端列表渲染为「主索引」模式（`currentResults` 为全量主数组，详情按主索引补全）。
> 数据源筛选引入 `viewIndices`（过滤后可见的主索引列表），分页/补全/翻页全部基于它，
> 因此**过滤不会破坏详情补全的索引映射**。边抓边合并只新增/补字段、不重排，索引稳定。

---

## 已知限制

- FC2 详情的**样品图**仅做低噪声的「图片链接」抓取（lightbox 形式），部分页面可能为空；
  封面来自 fc2ppvdb 缩略图。
- fc2ppvdb 页面结构若调整，标签字段（女优/贩卖日等）采用「文本前缀 + span」提取，
  对 Tailwind class 变动免疫，但若站点改版仍可能需要微调选择器。
