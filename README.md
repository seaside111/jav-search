# JAV Search · v1.5.0-beta

运行在群晖 Docker 上的日本成人影片信息检索 Web 应用。支持按番号 / 演员 / 关键词搜索，集成百度·阿里云翻译，接入 **Jackett 资源搜索**，支持 **一键推送 qBittorrent / Transmission 下载** 与 **下载完成自动刮削归档（Emby 兼容 NFO + 封面）**，并在 v1.5.0-beta 新增完整的 **发种到 PT（M-Team）流水线**。

> 🧪 **这是 beta 测试分支**（`beta` 分支 / `:beta` 镜像），用于提前体验 v1.5 新功能，可能存在未稳定项；求稳请用 [正式版 `main` 分支 / `:latest` 镜像](https://github.com/seaside111/jav-search)。两者互不影响、可并存。

---

## 功能

- 🔍 **三种搜索模式**：番号（如 `ABP-123`）、演员名、关键词，自动识别
- 🖼 **完整信息展示**：封面、演员头像、导演、出品商、发行商、系列、时长、发行日期、标签、简介
- 🌐 **多数据源**：JavBus + JavDB 为主，可选 AVSOX（无码）/ AVMOO（旧片）/ FC2（FC2-PPV 素人无码，V1.4.3），并发抓取、按番号去重合并
- 🧩 **FC2 封面/标题补全**（V1.4.3）：fc2ppvdb 缺图缺标题时自动用 **MissAV** 补全（封面走 fourhoi CDN，含下架条目）
- ⚡ **大批量 & 快速**：列表/详情抓取分离，单源最高 500 条，翻页时按需补全详情
- 🆕 **首页最新片源**：未搜索时自动展示数据源最新片源（可在设置关闭）；V1.4.3 起**各来源并行、边抓边显示**，并提供**数据源切换标签**快速筛选
- 🔔 **版本更新检测**（V1.4.3）：顶部显示当前版本，后端代理检测 GitHub 最新 release，有新版本时红点提醒
- 🈳 **翻译功能**：百度 / 阿里云翻译，每条结果独立翻译按钮
- 🧲 **Jackett 资源搜索**：每张影片卡片一键搜索磁力 / 种子资源
- 🚀 **下载器推送**（V1.4，V1.5 加 Transmission）：资源列表「推送下载」按钮，按当前下载器（qBittorrent / Transmission）直接加种到指定目录
- 📤 **发种到 PT（M-Team）**（V1.5-beta）：对资源磁力一键「发种」，后台流水线自动查重→下载→刮削→制种→图床→发布→取回官方种子做种，独立「发种中心」整页跟进任务与做种监控
- 🎬 **自动刮削归档**（V1.4）：监控下载目录，完成后写 Emby/Kodi NFO + 封面，重命名为番号，按 `YYYYMM/番号/` 归档
- ⚡ **迅雷下载**：资源列表点「迅雷」唤起本机迅雷；也可复制磁链 / 下载 .torrent
- ⚙ **可视化设置**：分栏配置代理、数据源、翻译、Jackett、下载器、刮削，无需改文件

---

## 快速部署（群晖）

```bash
# 1. 上传整个项目到群晖，如 /volume1/docker/jav-search
cd /volume1/docker/jav-search

# 2. 构建并启动（配置使用 Docker 命名卷，无需手动建目录）
docker-compose up -d --build

# 3. 查看日志
docker-compose logs -f
```

访问 `http://<群晖IP>:8085`。

> **部署前务必修改 docker-compose 里的 `AUTH_USERNAME` / `AUTH_PASSWORD` / `AUTH_SECRET`！**

### 不用 compose，纯 `docker run` 部署（含刮削目录）

不想用 compose 也行，下面是一条完整命令（已含媒体库刮削的目录挂载），改好账号密码即可直接执行：

```bash
docker run -d \
  --name jav-search \
  --restart unless-stopped \
  -p 8085:8085 \
  -v jav-config:/config                          `# 配置持久化（命名卷，删容器不丢配置）` \
  -v /volume1/downloads/jav:/data/downloads      `# 刮削目录：＝下载器保存目录/刮削监控源，发种就地规整与做种（冒号左侧改成你的真实路径）` \
  -v /volume1/media/jav:/data/media              `# 归档目录：刮削后归档给 EMBY 等媒体库（按 YYYYMM/番号/ 建子目录）` \
  -e CONFIG_DIR=/config \
  -e PORT=8085 \
  -e TZ=Asia/Shanghai \
  -e AUTH_USERNAME=admin                         `# 登录用户名` \
  -e AUTH_PASSWORD=改成你的强密码                  `# 登录密码，务必修改` \
  -e AUTH_SECRET=一串很长的随机字符串              `# 会话签名密钥，填长随机串` \
  -e AUTH_SESSION_TTL=604800 \
  --add-host host.docker.internal:host-gateway \
  ghcr.io/seaside111/jav-search:beta
```

> 上面 `` `# ...` `` 是行内批注，会被 shell 当空忽略，可整段直接复制运行。挂载冒号**左侧**是宿主机真实路径（按需改），**右侧**是容器内路径——现已统一到 `/data` 下（`/data/downloads` 刮削、`/data/media` 归档），需与「设置 → 刮削 / 发种」里填的容器路径一致。不需要刮削/发种功能就删掉中间两行 `-v`。Windows PowerShell 请去掉行尾 `\` 与批注、写成一行。

**升级 / 重建**（配置在命名卷里，不会丢）：

```bash
docker pull ghcr.io/seaside111/jav-search:beta
docker stop jav-search && docker rm jav-search
# 再次执行上面的 docker run 命令即可
```

> 🧪 beta 用户拉的是 `:beta` 移动标签（随 beta tag 更新）；想锁定到某个具体 beta 版本，把镜像换成 `:V1.5.0-beta` 即可。

### FlareSolverr（JavDB / FC2 过 Cloudflare 盾用 · 留空自动探测 / 也可手填）

JavDB / FC2 需要过 Cloudflare 盾。**FlareSolverr 不再打进本项目的安装包**——你只需在任意地方
自行跑一个 FlareSolverr，本应用会**自动找到它**，简单可控、互不绑定。

```bash
# 自己单独跑一个 FlareSolverr（任意主机/NAS 均可，端口默认 8191）
docker run -d --name flaresolverr --restart unless-stopped \
  -p 8191:8191 \
  -e LOG_LEVEL=info -e TZ=Asia/Shanghai \
  ghcr.io/flaresolverr/flaresolverr:latest
```

**最省事：「设置 → JavDB 反爬」的 FlareSolverr 地址栏留空即可** —— 程序会自动探测本机/同宿主机的
FlareSolverr（依次试容器名 / `host.docker.internal` / 网桥网关 / `127.0.0.1`，再扫描 docker 同网段的
8191），装了就自然连上，**无需理解 docker 网络、也不用手查容器 IP**。点「测试 JavDB 连通」会显示探到的地址。

也可以**手填具体 URL**固定使用（优先级最高），例如：

- 局域网：`http://192.168.1.100:8191`
- 绑定的外网域名：`https://fs.yourdomain.com`

填法很宽松：缺 `http://` 会自动补、不填端口默认 `8191`、带 `/v1` 或末尾斜杠都会自动清理。

> ⚠️ FlareSolverr 的出口 IP = 跑它的那台机器的 IP。若它在**机房 VPS（数据中心 IP）**上，
> JavDB 仍可能按 IP 信誉**硬封 403**——此时在「设置 → 主代理」填一个【**非日本**】住宅代理
> （保持「FlareSolverr 复用主代理」开启）。

> 🛡️ **防过载**：后端对发往 FlareSolverr 的请求做了串行闸 + 排队上限 + 连续失败熔断，
> 即使刮削/最新/搜索/连通测试同时触发，也只会一个一个排队，不会再把单实例挤到卡死。

---

## 登录认证（环境变量）

网页需登录才能访问，账号密码在 docker-compose 的 `environment` 中设置，不开放注册。

| 变量 | 说明 |
|------|------|
| `AUTH_USERNAME` | 登录用户名 |
| `AUTH_PASSWORD` | 登录密码（**留空则关闭认证**，仅建议内网调试时使用） |
| `AUTH_SECRET` | 会话签名密钥，填一串长随机字符串；修改它会让所有人重新登录 |
| `AUTH_SESSION_TTL` | 会话有效期（秒），默认 604800（7 天） |

```yaml
environment:
  - AUTH_USERNAME=admin
  - AUTH_PASSWORD=你的强密码
  - AUTH_SECRET=一串很长的随机字符串
  - AUTH_SESSION_TTL=604800
```

---

## 网络模式与代理（重要）

访问 JavBus / JavDB 通常需要科学上网。若搜索时日志出现 `ConnectTimeout`，多半是 **网络模式** 问题：默认 bridge 网络的容器路由不到其它网段的代理。

项目附带 `docker-compose.host.yml`，让容器与群晖共用网络栈：

```bash
docker-compose down
docker-compose -f docker-compose.host.yml up -d --build
```

| 项目 | bridge 模式（默认） | host 模式 |
|------|--------------------|-----------|
| 代理地址 | `http://host.docker.internal:7890` | `http://<代理IP>:7890` |
| Jackett 地址 | `http://host.docker.internal:9117` | `http://localhost:9117` 或 `http://<群晖IP>:9117` |
| qBittorrent 地址 | `http://host.docker.internal:8080` | `http://localhost:8080` 或 `http://<群晖IP>:8080` |

在「设置 → 常规 → 代理」填入代理地址，例如 `http://192.168.1.100:7890`。

---

## qBittorrent 推送（V1.4）

1. 在群晖里运行 qBittorrent，开启 WebUI。
2. 本应用「设置 → 下载器」：
   - **WebUI 地址**：如 `http://<群晖IP>:8080`（host 模式可用 `http://localhost:8080`）
   - **用户名 / 密码**：qBittorrent WebUI 的账号密码
   - **保存目录**：推送任务的下载保存目录（qB 主机视角），如 `/downloads/jav`
   - **任务分类**：便于在 qB 中筛选，可留默认 `jav`
3. 点「测试连接」，绿点表示正常，保存。
4. 搜索影片 → 卡片底部「搜索资源」→ 资源列表点 **「推送下载」**，磁力/种子直接加种。

---

## 自动刮削归档（V1.4）

监控下载目录，对下载完成的视频自动刮削，产出 Emby/Kodi 兼容的 NFO + 封面，并按年月归档。

### 目录映射

刮削容器需要能访问下载目录与归档目录。编辑 docker-compose，按需启用卷映射（**冒号右侧是容器内路径，要与设置里填的一致**）：

```yaml
volumes:
  - /volume1/downloads/jav:/downloads/jav   # 监控源（与 qBittorrent 保存目录同一份数据）
  - /volume1/media/jav:/media/jav           # 刮削后归档目录
```

### 设置 → 刮削

| 项 | 说明 |
|----|------|
| 启用后台自动刮削监控 | 总开关 |
| 监控目录 | 下载器保存目录（**容器内路径**，如 `/downloads/jav`） |
| 归档目录 | 刮削后移动到此，按 `YYYYMM/番号/` 建子目录 |
| 轮询间隔（秒） | 每隔多久扫描一次，默认 300 |
| 静置判定（秒） | 文件超过这么久没再写入即判定下载完成，默认 60 |
| 最小文件大小（MB） | 小于此值的视频忽略（样板/预告） |
| 刮削失败也照常移动归档 | 监控正常但刮不到内容时仍移动 |

### 行为说明

- **完成判定**：跳过 qB 未完成分片（`.!qB`）；文件静置超过阈值即处理（手动放入的文件也能快速识别）；大小连续不变作兜底。
- **NFO 标题**：番号（字母+数字）不翻译，作为前缀；仅日文片名/简介调用翻译，结果形如 `MOON-057 中文片名`。
- **重命名**：视频改名为 `番号.后缀`，去掉广告网址等无关字符。
- **归档布局**：`归档目录/YYYYMM/番号/`（含 `番号.mp4 / .nfo / -poster.jpg / -fanart.jpg`，Emby 单片单目录）。
- **清理原目录**：视频移走后，若原下载子目录已无其它达标视频，则整目录删除（含遗留样板/广告文件）。

---

## 翻译 API 配置

- **百度翻译**：注册 https://fanyi-api.baidu.com/ ，填 APP ID + 密钥（免费版每月 500 万字符）
- **阿里云翻译**：开通 https://mt.console.aliyun.com/ ，填 AccessKeyId + Secret（每月 100 万字符免费）

NFO 标题/简介的中文翻译即使用此处配置的翻译服务。

---

## 项目结构

```
jav-search/
├── Dockerfile
├── docker-compose.yml               # bridge 网络（默认）
├── docker-compose.host.yml          # host 网络（代理跨网段时用）
├── install.sh / install.ps1         # 纯 docker run 一键安装脚本（仅 jav-search，FlareSolverr 自备）
├── .github/workflows/docker-publish.yml  # 推 tag 自动构建多架构镜像（beta→:beta / 正式→:latest）
├── README.md
├── config/                     # 占位目录（用命名卷时可忽略）
├── backend/
│   ├── main.py                 # FastAPI 主程序（搜索/翻译/Jackett/qB/配置）
│   ├── requirements.txt
│   ├── config_manager.py       # 配置读写（含 FlareSolverr 地址 env 兜底）
│   ├── translator.py           # 百度/阿里云翻译
│   ├── jackett.py              # Jackett 资源搜索
│   ├── qbittorrent.py          # qBittorrent WebUI 客户端（推送下载）
│   ├── transmission.py / downloader.py  # Transmission 客户端 + 下载器调度抽象层（V1.5）
│   ├── mteam.py / mteam_enums.py        # M-Team PT 接入（搜索/发种/取种）+ 枚举映射（V1.5）
│   ├── publish.py              # 发种流水线状态机 + 队列 + 后台 worker（V1.5）
│   ├── mediainfo.py / screenshot.py / torrentmaker.py / imagehost.py  # 发种基建：探测/截图/制种/图床（V1.5）
│   ├── monitor.py / logbus.py  # 做种监控聚合 + 可切换详略日志（V1.5）
│   ├── library.py              # 媒体库刮削监控 + NFO/封面 + 归档移动
│   └── scrapers/
│       ├── __init__.py         # 聚合搜索（列表/详情分离）
│       ├── _fsgate.py          # FlareSolverr 串行闸 + 地址智能适配（三源共享）
│       ├── _javbus_base.py
│       ├── javbus.py / javdb.py / avsox.py / avmoo.py
│       ├── fc2.py               # FC2-PPV 数据源（fc2ppvdb.com，走 FlareSolverr）
│       └── _missav.py           # MissAV 补全（给 FC2 补封面/标题，fourhoi CDN 封面）
└── frontend/
    ├── index.html              # 单页前端（搜索 + 资源抽屉 + 推送 + 分栏设置）
    ├── publish.html            # 发种中心整页（发种任务 / 做种监控 / 发种设置，V1.5）
    └── login.html
```

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | Web 前端 |
| GET  | `/api/health` | 健康检查 |
| GET  | `/api/version` | 版本检测（当前版本 vs GitHub 最新 release，带缓存） |
| POST | `/api/search` | 搜索影片（列表） |
| POST | `/api/details` | 按需补全详情 |
| GET  | `/api/latest` | 首页最新片源（`?source=xxx` 可只抓单源，供边抓边显示） |
| GET  | `/api/javdb/test` | JavDB 连通诊断 |
| GET  | `/api/fc2/test` | FC2（fc2ppvdb）连通诊断 |
| POST | `/api/translate` | 翻译文本 |
| POST | `/api/jackett/search` | Jackett 资源搜索 |
| GET  | `/api/jackett/status` | Jackett 连接状态 |
| POST | `/api/qbittorrent/add` | 推送磁力/种子到 qBittorrent |
| GET  | `/api/qbittorrent/status` | qBittorrent 连接状态 |
| POST | `/api/library/scrape/run-once` | 立即手动扫描刮削一次 |
| GET  | `/api/library/scrape/monitor` | 刮削监控状态 |
| GET  | `/api/downloader/status` | 当前下载器（qB/TR）连接状态 |
| GET  | `/api/mteam/test` | M-Team 连通诊断 |
| POST | `/api/publish/enqueue` | 提交发种任务（V1.5） |
| GET  | `/api/publish/tasks` | 发种任务列表 / 状态（V1.5） |
| POST | `/api/publish/{id}/confirm` | 确认发布（人工闸门，V1.5） |
| GET  | `/api/monitor/global` | 做种监控：站点全局数据（V1.5） |
| GET  | `/api/monitor/seeds` | 做种监控：各做种任务（V1.5） |
| GET  | `/api/config` | 获取配置（密钥脱敏） |
| POST | `/api/config` | 保存配置 |

---

## 更新日志

> 🧪 **V1.5.0-beta 发种到 PT（大版本 · 测试中）**：在「检索 → 找种 → 下载」链路末端接入完整的 **发种到 M-Team（馒头）** 流水线。要点：① 下载器抽象层，新增 **Transmission**，推送下载按当前下载器自动切换；② 接入 **M-Team PT 站**（`x-api-key`，开发期默认连测试站，可在设置改地址/密钥）；③ **一键发种流水线**——对资源磁力点「发种」，后台状态机自动完成「查重→下载→刮削→制种→截图图床→组装简介→发布→取回官方种子做种」，两处查重防抢发、发布闸门默认人工确认、任务持久化可重试；④ 发种类型（清晰度/编码/有码无码/分类）按 mediainfo + 番号启发式**智能识别**；⑤ 独立 **「发种中心」整页**（发种任务 / 做种监控 / 发种设置）；⑥ 可切换详略的系统日志、单种上传限速。镜像因增装 ffmpeg/mediainfo 由 ~150MB 增至 ~400MB。详见 [`docs_v1.5.0_更新说明.md`](docs_v1.5.0_更新说明.md)。
>
> _beta 阶段提醒：发种流水线（尤其 createOredit 上传与官方种子取回做种）需在真实下载器 / M-Team 环境实测验证；详细日志默认开启便于排查，定型后将关闭。_

> **V1.4.4 FC2 专项优化**：① FC2「最新片源」改为**优先用 sukebei 发现番号**（种子站按 id 倒序＝最新、直连不过盾、最快），拿到 fc2ppvdb 够不到的市面最新号，并**统一按 FC2-PPV 编号降序**取最新；② 新增**后台串行预抓 MissAV**（直连不过盾、低负担），慢慢灌入最新片的真标题/封面/样品图，刷新后列表卡变干净、详情样品图秒出；③ FC2 详情改为 **MissAV-only**，点开即出、彻底不碰 FlareSolverr，不再与 JavDB/刮削抢实例（fc2ppvdb 仍保留用于关键词/女优名搜索与首页最新兜底）；④ 设置页新增「最新优先用 sukebei / 后台预抓样品图 + 条数 / fc2ppvdb 抓取页数」。详见 [`docs_v1.4.4_更新说明.md`](docs_v1.4.4_更新说明.md)。
>
> **V1.4.3 首页增强**：① 顶部新增**版本号 + 更新检测**（后端代理 GitHub release，带缓存，有新版红点提醒）；② 首页最新片源改为**各来源并行、边抓边显示**（`/api/latest?source=`），快源先显示、慢源随后补入，不再被慢源拖住整页；③ 列表上方新增**数据源切换标签**（全部/JavBus/JavDB/…/FC2，各带条数），首页与搜索结果均可一键筛选。详见 [`docs_v1.4.3_更新说明.md`](docs_v1.4.3_更新说明.md)。
>
> **V1.4.3 新增（FC2 数据源）**：① 新增 **FC2-PPV 专用数据源**（fc2ppvdb.com），收录 FC2 素人/无码片源的番号、标题、封面、女优、卖家、贩卖日、收录时间、标签等；② 番号自动识别扩展，`FC2-PPV-1234567` / `FC2PPV1234567` / 纯数字 均按番号检索，直命中详情页；③ fc2ppvdb 强制 Cloudflare Turnstile，复用 JavDB 那套 FlareSolverr 取页（FC2 地址留空则自动复用 JavDB 的 FlareSolverr）；④ 设置页新增 FC2 数据源勾选、FlareSolverr/Cookie 配置与「FC2 连通测试」。注意：FC2 站点不提供磁力，下载仍走 Jackett/sukebei 按 `FC2-PPV-xxxxxxx` 检索。详见 [`docs_v1.4.3_更新说明.md`](docs_v1.4.3_更新说明.md)。
>
> **V1.4.2 优化（JavDB 专项）**：① JavDB 反爬增强——自动携带 `over18`/`locale` Cookie + 完整浏览器请求头 + 失败重试，解决「列表 0 条」；② 可选 FlareSolverr 过 Cloudflare 盾，或手动填入 `cf_clearance` Cookie；③ 设置页新增「JavDB 连通测试」，返回是否可达 / 是否被 CF 拦截 / 出口 IP 所在国（判断是否需要日本 IP）/ 解析条数；④ JavDB 详情扩充磁力链、样品图、评分人数等字段，并在合并去重时补全到主条目。详见 [`docs_v1.4.2_更新说明.md`](docs_v1.4.2_更新说明.md)。

> **V1.4.1 修复**：① 推送下载时记录搜索结果的准确番号，刮削时优先采用，修复 `hhd800.com@390JAC-234` 这类带站点/数字前缀文件名被误判为 `HHD-800` 的问题（文件名正则也同步增强）；② 服务地址填 `http://localhost` 时，种子直链改由后端代取再转交浏览器/qBittorrent，修复外网点击 `.torrent` 打不开、推送种子失败的问题。详见 [`docs_v1.4.1_更新说明.md`](docs_v1.4.1_更新说明.md)。

> **V1.4 新增**：① 资源结果一键推送到 qBittorrent；② 设置界面分栏改版（常规 / 翻译 / Jackett / 下载器 / 刮削）；③ 媒体库自动刮削监控（番号识别→搜索→中文翻译标题→写 NFO/封面）；④ 刮削后按年月归档移动并清理原下载目录。详见 [`docs_v1.4_更新说明.md`](docs_v1.4_更新说明.md)。

---

## 常见问题

**Q: 影片搜索没结果？** JavBus/JavDB 在大陆需代理，请在「设置 → 常规 → 代理」配置；并确认网络模式可达代理。

**Q: Jackett / qBittorrent 测试连接失败？** 检查地址能否在群晖内网访问；服务跑在宿主机就用 `host.docker.internal`（bridge）或 `localhost`（host）；确认账号/Key 正确。

**Q: 刮削不启动 / 不移动？** 看 `docker-compose logs -f` 里的 `[刮削 ...]` 日志：`监控目录不存在` 说明卷映射或容器内路径不对；`等待下载完成` 属正常（等待静置判定）；确认「设置 → 刮削」已勾选启用并保存。

**Q: 迅雷按钮点了没反应？** 浏览器拦截了 `thunder://` 协议，需在浏览器允许；或用「磁链」复制后手动添加。

---

> 本项目仅用于个人学习与本地媒体库信息整理，请遵守所在地区法律法规。
