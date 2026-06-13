# JAV Search v1.5.0-beta 更新说明

> 大版本：在「检索 → 找种 → 下载」链路末端，接入完整的 **发种到 PT（M-Team）** 流水线。

## 一、下载器多后端 + 抽象层
- 新增 **Transmission** 下载器（RPC，含 `X-Transmission-Session-Id` 握手、Basic 鉴权、labels 标签）。
- 新增 `backend/downloader.py` 调度层：按设置项 `下载器类型` 在 qBittorrent / Transmission 间切换；现有「推送下载」自动走当前下载器。
- 设置 →「下载器」新增**当前下载器**选择器与 Transmission 连接配置。

## 二、M-Team（馒头）PT 站接入
- 新增 `backend/mteam.py`：`search / detail / files / genDlToken / createOredit(发种) / getConf / 下载.torrent / 诊断`。
- 鉴权 `x-api-key`；API 地址可配，开发期默认连**测试站** `https://test2.m-team.cc/api`（正式接口后续替换，改设置即可）。
- 设置「PT站」分栏：API 地址 / 密钥 / 测试连接。

## 三、发种流水线（核心）
入口在「资源搜索」结果里——展开某影片资源后，在某条磁力上点 **「发种」** 即加入队列。后台 worker 按状态机推进：
```
查重(站点已有则终止) → Jackett磁力下载 → 下载完停种 →
刮削规整(番号+日文原名/封面/NFO) → 制种(torf,source+private) → 删原磁力种 →
发布前复查(被抢发则终止) → 待发布(默认人工确认/可设自动) →
截图→pixhost图床→组装简介→createOredit发布 → genDlToken取回官方种子做种 →
做种(分享率/时长达标)→停止→(可选)删种/删文件
```
- 两处查重：下载前（必查）+ 发布前（防抢发）。
- 制种只写 `source`+private（infohash 固定），发布后取回官方种子做种，**无需预配 passkey/announce**。
- 并发：`同时活跃数` 控制流水线槽位，做种占槽直到停止条件，从而「达上限即排队」。
- **发布闸门默认人工确认**（在「发种」面板点「确认发布」），可设为自动。
- 任务持久化 `/config/publish_tasks.json`，重启续存；支持重试/删除。
- 新增「发种」面板（任务列表 + 状态 + 确认/重试/删除）。

### 配套基建模块
- `backend/mediainfo.py`（mediainfo 文本 + pymediainfo 解析）
- `backend/screenshot.py`（ffmpeg 等分截图 + contact sheet）
- `backend/torrentmaker.py`（torf 制种）
- `backend/imagehost.py`（pixhost 匿名上传，content_type=1 成人）
- `backend/publish.py`（流水线状态机 + 队列 + 后台 worker）
- `backend/mteam_enums.py`（getConf 枚举缓存 + 智能类型映射）
- `backend/monitor.py`（做种监控数据聚合）

### 发种类型智能识别
发种时自动填 M-Team 的 `category/standard/videoCodec/audioCodec`：
- 清晰度 standard ← mediainfo 高度（4K/1080p/720p/SD）
- 视频/音频编码 ← mediainfo 编码（H264/H265、AAC/AC3…）
- 有码/无码 category ← 来源/番号启发式（avsox·avmoo、FC2、无码厂牌、日期型番号 → 无码；余 → 有码）
- 从 `/system/getConf` 拉枚举缓存、按名称匹配 id；识别不到则兜底用设置里的发布分类 id。

## 五、独立「发种中心」页面 + 做种监控
- 顶部「发种」按钮进入独立整页 `/publish`（不再弹窗），三个标签：
  - **发种任务**：队列/状态/确认发布/重试/删除，自动刷新。
  - **做种监控**：站点全局数据（用户/上传/下载/分享率/魔力值/做种数）+ 每个做种任务（本地上传速度/已传/分享率/时长 + 站点审核状态）。
  - **发种设置**：M-Team 连接（含 uid）、工作目录&制种、发布参数、下载截图做种参数。
- 全局基础设置（代理/数据源/翻译/Jackett/下载器/刮削）仍在原弹窗设置。

## 七、可切换详略的日志
- `backend/logbus.py`：两档输出——**主要动作**（状态流转/成功/失败，始终显示）与**细节**（每一步 + 每次 M-Team API 调用，仅详细模式）。
- 设置 →「常规 → 系统日志 → 详细日志」开关（配置 `log_verbose`，默认开）。
- beta 测试期开启，docker logs 可看完整流程便于排查；**定型稳定后关闭，只剩主要动作**。

## 六、单种上传限速
- 设置 `单种上传限速(KB/s)`，做种时给该种子设上限（qB `upLimit`、TR `uploadLimit`+`uploadLimited`），0=不限。防超 PT 单种限速被封。

## 四、镜像依赖
- Dockerfile 增装 `ffmpeg`、`mediainfo`；`requirements.txt` 增 `torf`、`pymediainfo`。镜像 ~150MB → ~400MB。

## 暂缓
- **辅种（cross-seed）**：一致性检测麻烦、实用度低，暂不提供入口（`backend/crossseed.py` 代码保留备用）。

## 新增配置项
`downloader_type` · `tr_url/username/password/save_path/category` ·
`mteam_api_base/api_key/source_flag` · `crossseed_category`（发种做种分类）·
`publish_work_dir/work_dir_host` · `publish_category`(必填,发布分类id) ·
`publish_max_active` · `publish_stop_ratio/stop_hours` ·
`publish_delete_after_stop/delete_files` · `publish_screenshot_count` ·
`publish_auto`(发布闸门) · `publish_anonymous`

## 使用前置
1. 设置「PT站」填 M-Team API 密钥、**发布分类 id**（点「拉取站点分类」获取）、发种工作目录（容器+主机两视角）。
2. 配好下载器与 Jackett。
3. 搜片 → 展开资源 → 对目标磁力点「发种」→ 在「发种」面板跟进，到「待发布」点确认。

## 待 Docker 实测
本机无 ffmpeg/mediainfo/torf 与真实下载器/M-Team 环境，流水线逻辑已写好并通过导入/接口冒烟，需在容器内跑通验证（尤其 createOredit 上传字段、官方种子取回做种）。
