"""
配置管理 — 读写 config/settings.json
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.getenv("CONFIG_DIR", "/config")) / "settings.json"

DEFAULT_CONFIG = {
    "proxy": "",                         # HTTP代理，如 http://192.168.1.1:7890
    "sources": ["javbus", "javdb"],      # 启用的数据源: javbus/javdb/avsox/avmoo
    # V1.4.2：JavDB 反爬增强
    "javdb_flaresolverr_url": "",        # FlareSolverr 地址（如 http://192.168.1.100:8191），填了则 JavDB 走它过 CF 盾
    "javdb_flaresolverr_use_proxy": True, # FlareSolverr 是否复用主代理；其自带出口（如 WARP）时应关掉走直连
    "javdb_cookie": "",                  # 手动填入浏览器导出的 JavDB Cookie（如 cf_clearance=xxx; _jdb_session=yyy）
    "javdb_prefetch_extras": False,      # 翻页预取时是否顺带预取合并卡的 JavDB 样品图/磁力（默认关，开则更耗资源）
    # V1.4.3：FC2-PPV 数据源（fc2ppvdb.com，强制 Cloudflare Turnstile，必须走 FlareSolverr）
    "fc2_flaresolverr_url": "",          # FC2 专用 FlareSolverr 地址；留空则复用 javdb_flaresolverr_url
    "fc2_flaresolverr_use_proxy": True,  # FlareSolverr 是否复用主代理；其自带出口时关掉走直连
    "fc2_cookie": "",                    # 可选：手动填入 fc2ppvdb 的 Cookie（如 cf_clearance=xxx）
    # V1.4.3：用 MissAV 给 FC2 补全封面/标题/女优/标签（fc2ppvdb 对下架条目常缺这些）
    "fc2_missav_enabled": True,          # 是否启用 MissAV 补全
    "fc2_missav_base": "",               # MissAV 镜像（逗号分隔，留空用内置默认 missav.ws）
    # V1.4.4：FC2 最新片源抓取页数。实测 fc2ppvdb 首页 `/` 不支持翻页（page=2=page=1）、
    # 也忽略 per_page/limit，且一次就返回约 100 条（最新+人気混排）。故默认 1：取这一页后
    # 全量按编号降序、截取最新 N 条即可。此项仅对需登录的 /articles 兜底列表可能有效，
    # 保留作未来扩展；走 FlareSolverr 每页过盾较慢，硬上限 3。
    "fc2_latest_pages": 1,
    # V1.4.4：FC2 最新优先用 sukebei 发现（种子站按 id 倒序＝最新、直连不过盾、最快），
    # 能拿到 fc2ppvdb 新着列表够不到的最新号；sukebei 够量就跳过慢的 fc2ppvdb 首页。默认开。
    "fc2_latest_use_sukebei": True,
    # V1.4.4：后台预抓 FC2 最新的 MissAV（标题/封面/样品图，直连不过盾、低负担），
    # 串行+节流慢慢灌缓存：列表卡升级干净标题/封面、点开详情样品图秒出。女优/标签不预抓。
    "fc2_prefetch_missav": True,
    "fc2_prefetch_count": 20,            # 预抓最新多少条（0 关闭，硬上限 60）
    "baidu_app_id": "",                  # 百度翻译 AppID
    "baidu_secret_key": "",              # 百度翻译 SecretKey
    "aliyun_access_key_id": "",          # 阿里云 AccessKeyId
    "aliyun_access_key_secret": "",      # 阿里云 AccessKeySecret
    "default_translate_provider": "baidu",  # 默认翻译服务
    "results_per_page": 12,
    "max_results": 300,                  # 每个数据源最多抓取的列表条目数（V1.3 上限 500）
    # V1.3：首页最新片源
    "show_latest": True,                 # 未搜索时首页是否展示最新片源
    "latest_sources": ["javbus", "javdb"],  # 首页最新片源取自哪些来源
    "latest_per_source": 40,             # 默认每来源条数（未在 latest_limits 指定时用）
    "latest_limits": {                   # 各来源最新片源抓取上限（可单独调节）
        "javbus": 100,
        "javdb": 40,
        "avsox": 40,
        "avmoo": 40,
        "fc2": 60,                       # FC2 最新条数；走 sukebei 按需翻页发现，硬上限 100（见 fc2.FC2_LATEST_MAX）
    },
    # 资源搜索（磁力/种子）：默认用 sukebei.nyaa.si（直连、免配置）；
    # jackett_enabled=True 且配置了 Jackett 时，Jackett 优先、sukebei 兜底。
    "jackett_enabled": False,            # 资源搜索是否启用 Jackett（默认关＝只用 sukebei）
    # Jackett
    "jackett_url": "",                   # Jackett 地址，如 http://192.168.1.100:9117
    "jackett_api_key": "",               # Jackett API Key
    "jackett_indexers": "all",           # 索引器，多个用逗号分隔
    "jackett_timeout": 20,               # 搜索超时秒数
    # V1.5：下载器类型切换（qb=qBittorrent / transmission=Transmission）
    "downloader_type": "qb",             # 当前启用的下载器后端
    # 首次经「推送」入口（搜索结果磁力/种子直推下载器）添加的种子上传限速(KB/s)，0=不限。
    #   仅作用于此处首推，不影响 PT 发种（那条线另有 publish_upload_limit_kbps）。
    "magnet_upload_limit_kbps": 0,
    # V1.4：qBittorrent 下载器（群晖中部署）
    "qb_url": "",                        # qBittorrent WebUI 地址，如 http://192.168.1.100:8080
    "qb_username": "",                   # WebUI 用户名
    "qb_password": "",                   # WebUI 密码
    "qb_save_path": "",                  # 推送任务的保存目录（qB 主机视角），留空用 qB 默认
    "qb_category": "jav",                # 任务分类，便于刮削监控筛选；留空不分类
    "qb_paused": False,                  # 推送后是否暂停（先不下载）
    # V1.5：Transmission 下载器（与 qB 并列，由 downloader_type 选择）
    "tr_url": "",                        # Transmission RPC 地址，如 http://192.168.1.100:9091
    "tr_username": "",                   # RPC 用户名（可空）
    "tr_password": "",                   # RPC 密码（可空）
    "tr_save_path": "",                  # 推送任务保存目录（TR 主机视角），留空用 TR 默认
    "tr_category": "jav",                # 任务标签（labels），便于筛选；留空不打标签
    # V1.5：M-Team PT 站（辅种/发种）。开发期连测试站，正式接口后续替换。
    "mteam_api_base": "https://test2.m-team.cc/api",  # API 根地址（测试站）
    "mteam_api_key": "",                 # M-Team API 密钥（控制台→实验室→API密钥生成）
    "mteam_uid": "",                     # M-Team 用户 uid（个人页 URL 里的数字；监控全局数据用）
    "mteam_source_flag": "M-Team",       # 制种 source 标记（影响 infohash，发种用）
    "crossseed_category": "mteam",       # 发种做种打的分类/标签（受种子管理保护，永不自动删）
    # V1.5：发种流水线（seed-in-place + 硬链接归档）
    # 刮削目录＝本项目容器(/data)里、实际指向下载器保存数据同一物理目录的挂载名：
    #   规整在此目录原地完成、做种也留原地。下载器的下载目录沿用全局下载器设置(qb_save_path 等)，
    #   首次下磁力与最后重新做种由下载器按它自己的保存目录处理，发种这里不再单独设。
    "publish_work_dir": "",              # 刮削目录（本项目容器视角）：读写/规整/归档文件用
    "publish_work_dir_host": "",         # 【已弃用·可留空】做种 save_path 兜底；现优先取种子自报 save_path，
                                         #   再兜底全局下载器默认保存目录，故一般无需填
    # 刮削开关（发种流水线）：开=文件名只留番号 + 标题/演员/简介写 NFO + 抓封面；关=保留原文件名、不写NFO/封面
    "publish_scrape_enabled": True,
    # 归档开关 + 模式：开=额外造一份到归档目录给 EMBY（与做种解耦，做种始终留下载目录）
    #   hardlink=同卷硬链接(不占空间)、跨卷自动退化为复制；copy=始终复制
    "publish_archive_enabled": True,
    "publish_archive_mode": "hardlink",  # hardlink | copy
    "publish_archive_by_month": True,    # 归档是否按年月建子目录（归档目录/YYYYMM/番号/）
    "publish_archive_dir": "",           # 归档目录（容器视角，给 EMBY 扫）：留空则回退 scrape_output_dir
    "publish_archive_dir_host": "",      # 【已弃用】seed-in-place 后做种不再经归档目录，此项不再使用
    "publish_max_active": 3,             # 同时活跃任务数（含做种）；超限排队
    "publish_stop_ratio": 1.0,           # 做种停止：分享率达此值（0=不按分享率停）
    "publish_stop_hours": 72,            # 做种停止：做种时长达此小时（0=不按时长停）
    "publish_delete_after_stop": False,  # 停止后是否删除做种任务
    "publish_delete_files": False,       # 删除做种时是否连同文件（危险，默认否）
    "publish_screenshot_count": 6,       # 发种截图张数
    # 优先图床：发种简介图按此图床优先上传，失败自动兜底其它。
    #   catbox(免key,但封机房/代理IP) / pixhost(免key,部分代理不通) / imgbb(需key,机房代理最稳)
    "image_host": "catbox",              # imgbb | imgchest | freeimage | catbox | pixhost（postimage 已移除：key 版接口被官方停用）
    "image_imgbb_key": "",               # imgbb API Key（https://api.imgbb.com 免费申请）
    "image_imgchest_token": "",          # imgchest 个人 access token（imgchest.com 账号→API 生成）；允许 NSFW、直链走 Cloudflare
    "image_freeimage_key": "",           # freeimage.host API key，留空用内置公开 key；直链 iili.io 走 Cloudflare
    "image_postimage_key": "",           # 【已弃用·保留兼容】postimage 已移除，此项不再使用
    "publish_auto": False,               # 发布闸门：False=人工确认，True=复查通过自动发布
    "publish_anonymous": False,          # 是否匿名发布
    "publish_category": "",              # 发布分类 id（必填，从 /torrent/categoryList 取）；手填优先于智能识别
    "publish_countries": "",             # 国家/地区 id（createOredit countries 字段，从 /system/countryList 取）
    "publish_poll_interval": 30,         # 发种 worker 轮询间隔（秒）
    "publish_upload_limit_kbps": 0,      # 单个发种种子的上传限速(KB/s)，0=不限。防超 PT 单种限速被封
    # V1.5：日志详略。True=详细(每步+每次API,beta排查用)；定型后设 False 只看主要动作
    "log_verbose": True,
    # V1.4：媒体库刮削（监控下载目录 → 刮削 → 移动归档）
    "scrape_enabled": False,             # 是否启用后台自动刮削监控
    "scrape_watch_dir": "",              # 监控目录（下载器保存的目录，容器内视角）
    "scrape_output_dir": "",             # 刮削后归档目录（按 YYYYMM 建子目录存放）
    "scrape_interval": 300,              # 监控轮询间隔（秒）
    "scrape_settle_seconds": 60,         # 文件 mtime 静置超过此秒数即判定下载完成（快速通道）
    "scrape_stable_checks": 2,           # 兜底：大小连续多少次不变视为完成（mtime 不可靠时）
    "scrape_min_size_mb": 100,           # 小于此大小（MB）的视频忽略（样板/预告）
    "scrape_translate_enabled": True,    # 刮削时是否翻译标题/简介；关闭则直接用日文原标题写入 NFO
    "scrape_translate_provider": "",     # 刮削翻译服务，留空用默认翻译服务
    "scrape_move_on_fail": True,         # 刮削失败也照常移动归档
}

# 列表抓取硬上限，防止配置过大拖垮服务
MAX_RESULTS_HARD_CAP = 500


# 环境变量兜底：键 -> 环境变量名。当 settings.json 里该项为空时用环境变量填充，
# 便于「一键 docker run 安装包」通过 compose env 预置 FlareSolverr 地址（用户零配置即可用）；
# 用户一旦在设置页填了值，settings.json 非空 → 仍以 UI 为准，env 不覆盖。
_ENV_FALLBACKS = {
    "javdb_flaresolverr_url": "JAVDB_FLARESOLVERR_URL",
    "fc2_flaresolverr_url": "FC2_FLARESOLVERR_URL",
}


def _apply_env_fallbacks(config: dict) -> dict:
    for key, env_name in _ENV_FALLBACKS.items():
        if not (config.get(key) or "").strip():
            env_val = (os.getenv(env_name) or "").strip()
            if env_val:
                config[key] = env_val
    return config


def load() -> dict:
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config = {**DEFAULT_CONFIG, **saved}
            return _apply_env_fallbacks(config)
    except Exception as e:
        print(f"[Config] load error: {e}")
    return _apply_env_fallbacks(dict(DEFAULT_CONFIG))


def save(config: dict) -> bool:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged = {**DEFAULT_CONFIG, **config}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Config] save error: {e}")
        return False
