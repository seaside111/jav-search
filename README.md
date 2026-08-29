# JAV Search

面向群晖及其他 Docker 环境的影片检索、下载与媒体库整理 Web 应用。将多来源信息搜索、资源发现、下载器推送、自动刮削归档和 Emby 媒体库适配整合在一个可视化界面中。

---

## 功能

- 🔍 **多来源影片检索**：支持番号、演员和关键词搜索，聚合 JavBus、JavDB、AVSOX、AVMOO、FC2 等来源，并按需补全封面、样品图和影片详情。
- 🆕 **最新片源浏览**：首页并行加载各数据源的最新内容，支持按来源筛选；列表与详情分离加载并复用缓存，兼顾速度和信息完整度。
- 🧲 **资源搜索与下载**：通过 Jackett 查找磁力链和种子，可复制链接、调用迅雷，或一键推送到 qBittorrent、Transmission。
- 📋 **任务状态管理**：按番号集中展示下载、刮削、归档和失败状态，便于跟踪处理进度及重试结果。
- 🎬 **自动刮削与归档**：监控下载目录，识别单片、多分段及字幕文件，生成 Emby/Kodi 兼容的 NFO、`poster.jpg`、`fanart.jpg`，并按自定义规则整理媒体文件。
- 👤 **演员与媒体库同步**：下载本地演员头像并写入影片目录，可将本地图片同步为 Emby 演员主图；支持字幕封面角标及历史归档修复。
- 🌐 **多渠道翻译**：支持传统翻译 API、大模型翻译以及 OpenAI 兼容服务，可配置默认渠道、代理、模型和请求超时。
- ⚙️ **集中可视化配置**：在网页中管理数据源、代理、翻译、Jackett、下载器和刮削规则，并提供连接检测、日志和版本更新提醒。

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
  -v /volume1/downloads/jav:/data/downloads      `# 刮削目录：＝下载器保存目录/刮削监控源（冒号左侧改成你的真实路径）` \
  -v /volume1/media/jav:/data/media              `# 归档目录：刮削后归档给 EMBY 等媒体库（按 YYYYMM/番号/ 建子目录）` \
  -e CONFIG_DIR=/config \
  -e PORT=8085 \
  -e TZ=Asia/Shanghai \
  -e AUTH_USERNAME=admin                         `# 登录用户名` \
  -e AUTH_PASSWORD=改成你的强密码                  `# 登录密码，务必修改` \
  -e AUTH_SECRET=一串很长的随机字符串              `# 会话签名密钥，填长随机串` \
  -e AUTH_SESSION_TTL=604800 \
  --add-host host.docker.internal:host-gateway \
  ghcr.io/seaside111/jav-search:latest
```

> 上面 `` `# ...` `` 是行内批注，会被 shell 当空忽略，可整段直接复制运行。挂载冒号**左侧**是宿主机真实路径（按需改），**右侧**是容器内路径——已统一到 `/data` 下（`/data/downloads` 刮削、`/data/media` 归档），需与「设置 → 刮削」里填的容器路径一致。不需要刮削功能就删掉中间两行 `-v`。Windows PowerShell 请去掉行尾 `\` 与批注、写成一行。

> **host 模式**：若代理/下载器在其它网段、bridge 路由不到（搜索日志报 `ConnectTimeout`），把上面命令里的 `-p 8085:8085` 与 `--add-host host.docker.internal:host-gateway` 两行删掉，改成一行 `--network host`，容器即与宿主机共用网络栈（端口直接是宿主机的 8085）。此时代理/Jackett/qB 地址改用 `http://localhost:端口` 或 `http://<群晖IP>:端口`，详见下方「网络模式与代理」。

**升级 / 重建**（配置在命名卷里，不会丢）：

```bash
docker pull ghcr.io/seaside111/jav-search:latest
docker stop jav-search && docker rm jav-search
# 再次执行上面的 docker run 命令即可
```

> 当前正式版本为 `v1.4.6.18`，也可以将镜像标签 `:latest` 换成固定版本标签 `:v1.4.6.18`。

版本检测读取 GitHub 的最新正式 Release（`/releases/latest`），不是直接读取 GHCR 的 `:latest` 标签。正式版本必须推送版本 tag；GitHub Actions 会自动创建对应 Release，并把 tag 注入镜像的 `/api/version`。Beta Release 不会作为正式版本参与检测。

### FlareSolverr（JavDB / FC2 过 Cloudflare 盾用 · 留空自动探测 / 也可手填）

JavDB / FC2 需要过 Cloudflare 盾。**FlareSolverr 不打进本项目的安装包**——你只需在任意地方
自行跑一个 FlareSolverr，本应用会**自动找到它**，简单可控、互不绑定。

```bash
# 自己单独跑一个 FlareSolverr（任意主机/NAS 均可，端口默认 8191）
docker run -d --name flaresolverr --restart unless-stopped \
  -p 8191:8191 \
  -e LOG_LEVEL=warning -e TZ=Asia/Shanghai \
  ghcr.io/flaresolverr/flaresolverr:latest
```

> 🔐 不建议把 FlareSolverr 设为 `LOG_LEVEL=info`：其 INFO 日志会打印完整请求体，
> 其中可能包含主代理账号密码和站点 Cookie。排障时若临时开启，结束后应立即恢复为
> `warning`，并避免公开粘贴原始请求体。

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

项目附带 `docker-compose.host.yml`，让容器与群晖共用网络栈（纯 `docker run` 部署同理：删掉 `-p 8085:8085` 与 `--add-host …`，改用一行 `--network host`）：

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

## 下载器推送（qBittorrent / Transmission）

1. 在群晖里运行 qBittorrent（或 Transmission），开启 WebUI / RPC。
2. 本应用「设置 → 下载器」：
   - **当前下载器**：选 qBittorrent 或 Transmission
   - **WebUI/RPC 地址**：如 `http://<群晖IP>:8080`（host 模式可用 `http://localhost:8080`）
   - **用户名 / 密码**：下载器的账号密码
   - **保存目录**：推送任务的下载保存目录（下载器主机视角），如 `/downloads/jav`
   - **任务分类**：便于在下载器中筛选，可留默认 `jav`
   - **磁力推送限速**：给推送的种子设单种上传限速（KB/s），0=不限
   - **完成后删除磁力链种子（保留文件）**：开启后，经「推送」加入的磁力链下载完成即删除种子记录、保留文件（磁力只用于下载、不做种）。只影响本项目推送的种子，不动你手动添加的。
3. 点「测试连接」，绿点表示正常，保存。
4. 搜索影片 → 卡片底部「搜索资源」→ 资源列表点 **「推送下载」**，磁力/种子直接加种。

---

## 自动刮削归档

监控下载目录，对下载完成的视频自动刮削，产出 Emby/Kodi 兼容的 NFO + 封面，并按年月归档。

**经「推送下载」加入的影片**：刮削时**直接复用列表里已呈现的元数据**（番号/封面/标题/演员/标签/简介），
不再从文件名重新识别番号 + 重新联网刮削——纯数字番号（如 AVSOX `061326_01`）也不会刮错；
推送时若详情还没加载完，刮削会回到该条目**自己的数据源**按其 url 补抓缺失字段（命中缓存则直接用），
绝不按番号盲目搜索串到别的来源。手动放进下载目录的文件则仍按文件名识别番号后刮削。

### 目录映射

刮削容器需要能访问下载目录与归档目录。编辑 docker-compose，按需启用卷映射（**冒号右侧是容器内路径，要与设置里填的一致**）：

```yaml
volumes:
  - /volume1/downloads/jav:/data/downloads   # 监控源（与下载器保存目录同一份数据）
  - /volume1/media/jav:/data/media           # 刮削后归档目录
```

### 设置 → 刮削

| 项 | 说明 |
|----|------|
| 刮削（改名番号 + 写 NFO/封面） | 总开关 |
| 归档（成品放到归档目录供 EMBY） | 总开关 |
| 下载/工作目录 | 下载器保存目录（**容器内路径**，如 `/data/downloads`） |
| 归档目录 | 刮削后移动到此，按 `YYYYMM/番号/` 建子目录 |
| 轮询间隔（秒） | 每隔多久扫描一次，默认 300 |
| 静置判定（秒） | 文件超过这么久没再写入即判定下载完成，默认 60 |
| 最小文件大小（MB） | 小于此值的视频忽略（样板/预告） |
| 刮削失败也照常移动归档 | 监控正常但刮不到内容时仍移动 |

### 行为说明

- **完成判定**：跳过 qB 未完成分片（`.!qB`）；文件静置超过阈值即处理（手动放入的文件也能快速识别）；大小连续不变作兜底。
- **NFO 标题**：番号（字母+数字）不翻译，作为前缀；仅日文片名/简介调用翻译，结果形如 `MOON-057 中文片名`。
- **重命名**：视频改名为 `番号.后缀`，去掉广告网址等无关字符。
- **归档布局**：`归档目录/YYYYMM/番号/`；图片统一为 `poster.jpg / fanart.jpg`（不带番号），每个视频的 NFO 与视频文件同名（分段视频分别对应各自 NFO），Emby 单片单目录。
- **清理原目录**：视频移走后，若原下载子目录已无其它达标视频，则整目录删除（含遗留样板/广告文件）。

---

## 翻译 API 配置

- **百度翻译**：注册 https://fanyi-api.baidu.com/ ，填 APP ID + 密钥（免费版每月 500 万字符）
- **阿里云翻译**：开通 https://mt.console.aliyun.com/ ，填 AccessKeyId + Secret（每月 100 万字符免费）

NFO 标题/简介的中文翻译即使用此处配置的翻译服务。

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | Web 前端 |
| GET  | `/api/health` | 健康检查 |
| GET  | `/api/version` | 版本检测（当前版本 vs GitHub 最新 release，带缓存） |
| POST | `/api/search` | 搜索影片（列表） |
| POST | `/api/details` | 按需补全详情（按 url 缓存，命中直接返回） |
| GET  | `/api/latest` | 首页最新片源（`?source=xxx` 可只抓单源，供边抓边显示） |
| GET  | `/api/img` | 图片代理（封面/样品图，内存+磁盘缓存） |
| GET  | `/api/javdb/test` | JavDB 连通诊断 |
| GET  | `/api/fc2/test` | FC2（fc2ppvdb）连通诊断 |
| POST | `/api/translate` | 翻译文本 |
| POST | `/api/jackett/search` | Jackett 资源搜索 |
| GET  | `/api/jackett/status` | Jackett 连接状态 |
| POST | `/api/qbittorrent/add` | 推送磁力/种子到下载器 |
| GET  | `/api/qbittorrent/status` | qBittorrent 连接状态 |
| GET  | `/api/downloader/status` | 当前下载器（qB/TR）连接状态 |
| POST | `/api/library/scrape/run-once` | 立即手动扫描刮削一次 |
| GET  | `/api/library/scrape/monitor` | 刮削监控状态 |
| GET  | `/api/config` | 获取配置（密钥脱敏） |
| POST | `/api/config` | 保存配置 |

---

## 常见问题

**Q: 影片搜索没结果？** JavBus/JavDB 在大陆需代理，请在「设置 → 常规 → 代理」配置；并确认网络模式可达代理。

**Q: Jackett / qBittorrent 测试连接失败？** 检查地址能否在群晖内网访问；服务跑在宿主机就用 `host.docker.internal`（bridge）或 `localhost`（host）；确认账号/Key 正确。

**Q: 刮削不启动 / 不移动？** 看 `docker-compose logs -f` 里的 `[刮削 ...]` 日志：`监控目录不存在` 说明卷映射或容器内路径不对；`等待下载完成` 属正常（等待静置判定）；确认「设置 → 刮削」已勾选启用并保存。

**Q: 迅雷按钮点了没反应？** 浏览器拦截了 `thunder://` 协议，需在浏览器允许；或用「磁链」复制后手动添加。

---

> 本项目仅用于个人学习与本地媒体库信息整理，请遵守所在地区法律法规。
