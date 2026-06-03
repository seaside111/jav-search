# JAV Search V1.4 更新说明

基于 V1.3 的大功能更新。四项新增能力：**qBittorrent 推送下载**、**设置界面分栏改版**、**媒体库自动刮削监控**、**刮削后按年月归档移动**。

---

## 1. 资源结果一键推送到 qBittorrent

在 Jackett 资源列表中，每条资源除「迅雷 / 磁链 / 种子」外新增 **「推送下载」** 按钮，直接把磁力链（优先）或 `.torrent` 链接推送到群晖里的 qBittorrent，并落到指定保存目录。

- 前端：`renderResourceItem()` 新增 `.btn-qb` 按钮 + `pushToQb()`（带推送中/已推送/失败态反馈）。
- 后端：
  - `backend/qbittorrent.py` —— qBittorrent WebUI 客户端：`/api/v2/auth/login` 取 SID Cookie（带 `Referer/Origin` 否则 403），`/api/v2/torrents/add` 加种（`urls` 字段同时支持磁力与 .torrent 直链）。
  - `POST /api/qbittorrent/add`（推送）、`GET /api/qbittorrent/status`（连通性+版本）。
- 设置项：`qb_url` / `qb_username` / `qb_password` / `qb_save_path` / `qb_category` / `qb_paused`。
  - 填 `qb_save_path` 时自动关闭 `autoTMM`（自动种子管理），保存目录才会生效。

## 2. 设置界面分栏改版

设置面板由「一长条」改为**顶部分栏标签 + 切换版块**：**常规 / 翻译 / Jackett / 下载器 / 刮削**。点击标签切换对应版块，底部为常驻「保存设置」栏（一次保存全部）。

- 新增 CSS：`.settings-tabs` / `.settings-tab(.active)` / `.settings-pane(.active)` / `.save-bar`。
- 新增 JS：`switchTab(name)`。`openSettings/saveSettings` 已扩展读写全部新字段。

## 3. 媒体库自动刮削监控

后台协程监控「下载器保存目录」，对**下载完成**的视频自动刮削，按 Emby/Kodi 规范产出**封面 + NFO**，NFO 标题/简介为**翻译后的中文**。

- 后端：`backend/library.py`
  - 完成检测：跳过 qBittorrent 未完成分片（同名 `.!qB`），文件大小连续 `scrape_stable_checks` 次不变才视为完成；小于 `scrape_min_size_mb` 的忽略。
  - 刮削流程：文件名提取番号 → `scrapers.search()`（JavBus/JavDB）→ 缺详情时 `enrich()` 补全 → 翻译标题+简介 → 写 `{stem}.nfo` + `{stem}-poster.jpg` + `{stem}-fanart.jpg`。
  - 监控协程 `_monitor_loop()`：按 `scrape_interval` 轮询；`scrape_enabled` 关闭即自行退出。启动事件 `start_monitor()` 拉起；保存设置后 `POST /api/library/scrape/monitor/refresh` → `ensure_monitor()` 热启停。
  - 路由：`GET /scrape/monitor`（状态）、`POST /scrape/monitor/refresh`、`POST /scrape/run-once`（立即扫一次）、`POST /scan`、`POST /scrape/single`（手动）。
- 前端「刮削」版块：开关、监控/归档目录、轮询间隔、最小大小、失败仍移动、状态指示、「立即扫描一次」、最近处理记录。
- 设置项：`scrape_enabled` / `scrape_watch_dir` / `scrape_output_dir` / `scrape_interval` / `scrape_stable_checks` / `scrape_min_size_mb` / `scrape_translate_provider` / `scrape_move_on_fail`。

## 4. 刮削后按年月归档移动

刮削结束后把视频及同名附属文件（`.nfo` / `-poster` / `-fanart`）移动到**归档目录 / 当前年月 /**（如 `202605`）。

- `_archive_file()`：在 `scrape_output_dir/YYYYMM/` 下创建目录并移动；重名自动加短随机后缀；移动后清理 `watch_dir` 内残留空子目录。
- **刮削失败也照常移动**：当 `scrape_move_on_fail=True`（默认）时，监控正常运行但无法刮到内容，仍执行相同的移动归档；未配置归档目录时只刮削不移动。

---

## 部署要点（群晖 Docker）

`docker-compose.yml` / `docker-compose.host.yml` 已预留刮削目录映射注释，按需启用并保证「设置→刮削」里填的是**容器内路径**（冒号右侧）：

```yaml
- /volume1/downloads/jav:/downloads/jav   # 监控源（与 qBittorrent 保存目录同一份数据）
- /volume1/media/jav:/media/jav           # 刮削后归档目录
```

- qBittorrent 跑在群晖宿主机时，bridge 模式可用 `host.docker.internal:8080` 访问；host 模式直接用 `127.0.0.1:8080`。
- 无新增 Python 依赖（仅用标准库 + 既有 `httpx`/`fastapi`），`requirements.txt` 不变。

## 日志诊断（V1.4 增补）

刮削/推送全流程已打点到容器日志（`docker logs jav-search`），统一前缀 `[刮削 HH:MM:SS]` / `[qB推送]` / `[启动]`：

- 监控启停、每轮扫描的目录与文件统计（共多少视频、等待稳定/下载中/过小/已处理）。
- 每个文件的判定：`等待下载稳定 1/2`、`判定下载完成`、`监控目录不存在`（排查卷映射/路径最有用）。
- 刮削细节：番号提取、命中影片、翻译、写 NFO/封面、归档移动的目标与文件清单。
- qB 推送：目标地址、保存目录、分类、成功/失败原因。

`_log()` 做了多重兜底，**任何日志编码错误都不会中断刮削或移动**；Dockerfile 增加 `PYTHONIOENCODING=utf-8` / `LANG=C.UTF-8` 确保中文日志正常输出。

> 注意：文件需「大小连续 N 次不变」才判定完成（默认 N=2），即下载完成后最多再过一个轮询间隔（默认 300s）才会开始刮削；日志会显示 `等待下载稳定 x/2`。

## 刮削细节增强（V1.4 增补 2）

- **NFO 标题**：番号（字母+数字，如 `MOON-057`）不翻译，作为 `<title>` 前缀；仅当片名/简介含**日文**时才调用翻译，结果形如 `MOON-057 中文片名`。原始日文标题保留在 `<originaltitle>`。
- **重命名为番号**：刮削后视频统一改名为 `番号.后缀`，去掉文件名里的广告网址/无关描述（如 `hhd800.com@JERA-043-uncen[ad].mp4` → `JERA-043.mp4`）。
- **单片单目录归档**：移动到 `归档目录/YYYYMM/番号/` 下，目录内含 `番号.mp4 / 番号.nfo / 番号-poster.jpg / 番号-fanart.jpg`（Emby 推荐布局）。
- **清理原下载目录**：视频移走后，若其所在的下载子目录已无其它达标视频，则**整目录删除**（连同样板片、广告 txt/jpg 等遗留文件）；同一子目录有多个视频时，待最后一个处理完再删；视频直接位于监控根目录时不删根目录。
- **更快更准的完成判定**（取代「必须连续 2 次大小不变」）：
  1. 仍有 qB 未完成分片标记 `.!qB` → 判为下载中，跳过；
  2. 文件 `mtime` 静置超过 `scrape_settle_seconds`（默认 60s）不再写入 → **立即处理**（单次扫描即可，手动放入的文件也据此快速识别）；
  3. 兜底：大小连续 `scrape_stable_checks` 次不变（应对网络存储 mtime 不可靠）。
  - 想被更快刮削可调小「轮询间隔」与「静置判定」。

## 关键文件

| 文件 | 改动 |
|------|------|
| `backend/qbittorrent.py` | 新增：qB WebUI 客户端 |
| `backend/library.py` | 新增：刮削 + NFO/封面 + 监控协程 + 年月归档移动 |
| `backend/config_manager.py` | 新增 qb_* / scrape_* 默认配置 |
| `backend/main.py` | 注册 library 路由、qB 端点、启动事件、配置变更热启停监控、版本 1.4.0 |
| `frontend/index.html` | 分栏设置、推送下载按钮、qB/刮削版块与逻辑 |
