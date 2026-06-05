# JAV Search

运行在群晖 Docker 上的日本成人影片信息检索 Web 应用。支持按番号 / 演员 / 关键词搜索，集成百度·阿里云翻译，接入 **Jackett 资源搜索**，并支持 **一键推送 qBittorrent 下载** 与 **下载完成自动刮削归档（Emby 兼容 NFO + 封面）**。

---

## 功能

- 🔍 **三种搜索模式**：番号（如 `ABP-123`）、演员名、关键词，自动识别
- 🖼 **完整信息展示**：封面、演员头像、导演、出品商、发行商、系列、时长、发行日期、标签、简介
- 🌐 **多数据源**：JavBus + JavDB 为主，可选 AVSOX（无码）/ AVMOO（旧片），并发抓取、按番号去重合并
- ⚡ **大批量 & 快速**：列表/详情抓取分离，单源最高 500 条，翻页时按需补全详情
- 🆕 **首页最新片源**：未搜索时自动展示数据源最新片源（可在设置关闭）
- 🈳 **翻译功能**：百度 / 阿里云翻译，每条结果独立翻译按钮
- 🧲 **Jackett 资源搜索**：每张影片卡片一键搜索磁力 / 种子资源
- 🚀 **qBittorrent 推送**（V1.4）：资源列表「推送下载」按钮，磁力/种子直接加种到指定保存目录
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
├── docker-compose.yml          # bridge 网络（默认）
├── docker-compose.host.yml     # host 网络（代理跨网段时用）
├── README.md
├── config/                     # 占位目录（用命名卷时可忽略）
├── backend/
│   ├── main.py                 # FastAPI 主程序（搜索/翻译/Jackett/qB/配置）
│   ├── requirements.txt
│   ├── config_manager.py       # 配置读写
│   ├── translator.py           # 百度/阿里云翻译
│   ├── jackett.py              # Jackett 资源搜索
│   ├── qbittorrent.py          # qBittorrent WebUI 客户端（推送下载）
│   ├── library.py              # 媒体库刮削监控 + NFO/封面 + 归档移动
│   └── scrapers/
│       ├── __init__.py         # 聚合搜索（列表/详情分离）
│       ├── _javbus_base.py
│       └── javbus.py / javdb.py / avsox.py / avmoo.py
└── frontend/
    ├── index.html              # 单页前端（搜索 + 资源抽屉 + 推送 + 分栏设置）
    └── login.html
```

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | Web 前端 |
| GET  | `/api/health` | 健康检查 |
| POST | `/api/search` | 搜索影片（列表） |
| POST | `/api/details` | 按需补全详情 |
| GET  | `/api/latest` | 首页最新片源 |
| POST | `/api/translate` | 翻译文本 |
| POST | `/api/jackett/search` | Jackett 资源搜索 |
| GET  | `/api/jackett/status` | Jackett 连接状态 |
| POST | `/api/qbittorrent/add` | 推送磁力/种子到 qBittorrent |
| GET  | `/api/qbittorrent/status` | qBittorrent 连接状态 |
| POST | `/api/library/scrape/run-once` | 立即手动扫描刮削一次 |
| GET  | `/api/library/scrape/monitor` | 刮削监控状态 |
| GET  | `/api/config` | 获取配置（密钥脱敏） |
| POST | `/api/config` | 保存配置 |

---

## 更新日志

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
