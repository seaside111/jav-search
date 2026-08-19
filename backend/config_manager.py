"""
配置管理 — 读写 config/settings.json
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.getenv("CONFIG_DIR", "/config")) / "settings.json"

DEFAULT_CONFIG = {
    "proxy": "",                         # HTTP代理，如 http://192.168.1.1:7890
    "sources": ["javbus", "javdb"],      # 可选: javbus/javdb/avsox/avmoo/jav321/dmm
    "dmm_api_id": "",                    # DMM/FANZA 官方联盟 API ID
    "dmm_affiliate_id": "",              # DMM/FANZA Affiliate ID
    # V1.4.2：JavDB 反爬增强
    "javdb_flaresolverr_url": "",        # FlareSolverr 地址（如 http://192.168.1.100:8191），填了则 JavDB 走它过 CF 盾
    "javdb_flaresolverr_use_proxy": True, # FlareSolverr 是否复用主代理；其自带出口（如 WARP）时应关掉走直连
    "javdb_cookie": "",                  # 手动填入浏览器导出的 JavDB Cookie（如 cf_clearance=xxx; _jdb_session=yyy）
    "javdb_prefetch_extras": False,      # 兼容旧配置；首页不再自动预取 JavDB 补充详情
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
    "magnet_upload_limit_kbps": 0,
    # 开：经「推送」加入的磁力链种子下载完成后，自动删除该种子记录（保留已下载文件）。
    #   这里的磁力链只用于下载文件、不做种，故下完即可移除种子，免得一直占着挂在下载器里。
    "magnet_delete_completed": False,
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
    # V1.5：日志详略。True=详细(每步+每次API,beta排查用)；定型后设 False 只看主要动作
    "log_verbose": True,
    # V1.4：媒体库刮削（监控下载目录 → 刮削 → 移动归档）
    "scrape_enabled": False,             # 【已废弃为独立开关】监控改由 scrape_meta_enabled/archive_enabled 任一开启自动运行，此键不再起作用
    "scrape_watch_dir": "",              # 监控目录（下载器保存的目录，容器内视角）
    "scrape_output_dir": "",             # 刮削后归档目录（按 YYYYMM 建子目录存放）
    "scrape_interval": 300,              # 监控轮询间隔（秒）
    "scrape_settle_seconds": 300,        # 非下载器整目录至少连续静置此秒数；兼容迅雷预分配且无临时后缀
    "scrape_stable_checks": 2,           # 非下载器文件大小/时间连续稳定次数；qB/TR 匹配任务优先使用 API
    "scrape_min_size_mb": 100,           # 小于此大小（MB）的视频忽略（样板/预告）
    "scrape_keep_size_mb": 300,          # 达到此大小一律视为正片，绝不作为广告删除
    "scrape_translate_enabled": True,    # 刮削时是否翻译标题/简介；关闭则直接用日文原标题写入 NFO
    "scrape_translate_provider": "",     # 刮削翻译服务，留空用默认翻译服务
    "scrape_move_on_fail": True,         # 刮削失败也照常归档
    # 刮削归档统一：监控目录中的成品按同一套规则写入媒体库目录。
    "archive_mode": "hardlink",          # hardlink | copy | move —— 监控孤儿下载的归档方式：
                                         #   hardlink/copy 保留原文件；move 移动并清理原下载目录。
                                         #   hardlink/copy 保留原文件；move 移走后安全清理空目录
    "archive_by_month": True,            # 归档是否按年月建子目录（归档目录/YYYYMM/所选文件夹名/）
    # 刮削/归档总开关：
    "scrape_meta_enabled": True,         # 刮削：写 NFO/封面；视频是否改名由独立开关控制
    "scrape_organize_enabled": True,     # 规整命名总开关；与刮削、广告删除独立
    "scrape_video_rename_enabled": True, # 关闭时归档仍执行，但视频保留原文件名
    "scrape_delete_extras": True,        # 独立控制删除判定为广告/赠片的视频；默认开启，可手动关闭
    "scrape_folder_naming": "code",      # code | actor | code_title | code_actor | code_title_actor
    "scrape_folder_title_translate": False,
    "scrape_folder_actor_mode": "first", # first | all
    "scrape_actor_subfolder_naming": "code", # code | code_title
    "scrape_jacket_artwork_enabled": True, # 封套同时生成裁切 poster 与完整横向 fanart；关闭则不裁切
    "scrape_actor_images_enabled": False,
    "scrape_actor_thumb_in_nfo": True,  # 在 NFO actor/thumb 中写入远程头像地址（Kodi/可移植性）
    "scrape_actor_images_dir": "",       # Emby metadata/people 路径
    "actor_scrape_auto": True,
    "actor_scrape_lookup_by_code": True,
    "actor_scrape_write_nfo": True,
    # AVMOO 与 AVSOX 共用后端；头像任务不请求需要过盾的 JavDB。
    "actor_scrape_sources": ["javbus", "avsox"],
    "actor_scrape_cache_dir": "",
    "actor_scrape_interval_seconds": 2.0,  # 批量/逐演员请求冷却，避免持续压垮来源与过盾服务
    "actor_javdb_directory_enabled": True, # 仅为首轮失败演员低频扫描 JavDB 演员目录
    "actor_javdb_directory_interval_hours": 12,
    "emby_url": "",
    "emby_api_key": "",
    "emby_media_root": "",             # Emby 容器内看到的归档根路径；留空表示与本容器一致
    "emby_actor_sync_enabled": False,
    "public_trackers": "",               # 用户自定义；每行或逗号一个，非空时覆盖自动列表
    "public_trackers_auto_update": True,  # 自动抓取在线 best 列表并缓存 7 天
    "archive_enabled": True,             # 归档：把监控目录成品放进归档目录供 EMBY 使用
}

# 列表抓取硬上限，防止配置过大拖垮服务
MAX_RESULTS_HARD_CAP = 500

# 已从当前版本移除的无效补图设置及发种/图床配置。读取旧 settings.json 时
# 先使用其中少量目录/开关别名完成一次迁移，再剔除全部旧键。
REMOVED_CONFIG_KEYS = {
    "scrape_artwork_fallback_limit",
    "mteam_api_base", "mteam_api_key", "mteam_uid", "mteam_source_flag",
    "crossseed_category", "publish_work_dir", "publish_work_dir_host",
    "publish_max_active", "publish_stop_ratio", "publish_stop_hours",
    "publish_delete_after_stop", "publish_delete_files", "publish_screenshot_count",
    "image_host", "image_imgbb_key", "image_imgchest_token", "image_freeimage_key",
    "image_postimage_key", "publish_auto", "publish_anonymous", "publish_category",
    "publish_countries", "publish_poll_interval", "publish_upload_limit_kbps",
    "publish_scrape_enabled", "publish_archive_enabled", "publish_archive_mode",
    "publish_archive_by_month", "publish_archive_dir", "publish_archive_dir_host",
}


# 环境变量兜底：键 -> 环境变量名。当 settings.json 里该项为空时用环境变量填充，
# 便于「一键 docker run 安装包」通过 compose env 预置 FlareSolverr 地址（用户零配置即可用）；
# 用户一旦在设置页填了值，settings.json 非空 → 仍以 UI 为准，env 不覆盖。
_ENV_FALLBACKS = {
    "javdb_flaresolverr_url": "JAVDB_FLARESOLVERR_URL",
    "fc2_flaresolverr_url": "FC2_FLARESOLVERR_URL",
    "dmm_api_id": "DMM_API_ID",
    "dmm_affiliate_id": "DMM_AFFILIATE_ID",
}


def _apply_env_fallbacks(config: dict) -> dict:
    for key, env_name in _ENV_FALLBACKS.items():
        if not (config.get(key) or "").strip():
            env_val = (os.getenv(env_name) or "").strip()
            if env_val:
                config[key] = env_val
    return config


def _migrate_unify_archive(config: dict, saved: dict) -> dict:
    """V1.5 统一：把旧版发种独立的「刮削目录/归档目录/归档模式/按年月」迁移到全局键。
    刮削监控与发种从此共用同一下载目录与归档行为，杜绝两套配置、两头执行。
    幂等：全局键一旦有值就不再覆盖；只在全局键缺省、而旧 publish_* 有用户值时回填。
    （只改运行时 config，不强制落盘；用户在统一后的设置页保存一次即固化。）"""
    # 目录：全局留空且旧发种目录有值 → 回填
    if not (config.get("scrape_watch_dir") or "").strip() and (saved.get("publish_work_dir") or "").strip():
        config["scrape_watch_dir"] = saved["publish_work_dir"]
    if not (config.get("scrape_output_dir") or "").strip() and (saved.get("publish_archive_dir") or "").strip():
        config["scrape_output_dir"] = saved["publish_archive_dir"]
    # 归档模式/按年月：用户从未设过全局键、但设过旧发种键 → 沿用旧值
    if "archive_mode" not in saved and saved.get("publish_archive_mode"):
        config["archive_mode"] = saved["publish_archive_mode"]
    if "archive_by_month" not in saved and "publish_archive_by_month" in saved:
        config["archive_by_month"] = saved["publish_archive_by_month"]
    # 刮削/归档总开关：从旧的发种专属开关迁移
    if "scrape_meta_enabled" not in saved and "publish_scrape_enabled" in saved:
        config["scrape_meta_enabled"] = saved["publish_scrape_enabled"]
    if "scrape_organize_enabled" not in saved:
        config["scrape_organize_enabled"] = saved.get(
            "scrape_meta_enabled", saved.get("publish_scrape_enabled", True))
    if "archive_enabled" not in saved and "publish_archive_enabled" in saved:
        config["archive_enabled"] = saved["publish_archive_enabled"]
    return config


def _without_removed_keys(config: dict) -> dict:
    return {key: value for key, value in config.items()
            if key not in REMOVED_CONFIG_KEYS}


def load() -> dict:
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config = {**DEFAULT_CONFIG, **_without_removed_keys(saved)}
            config = _migrate_unify_archive(config, saved)
            return _apply_env_fallbacks(config)
    except Exception as e:
        print(f"[Config] load error: {e}")
    return _apply_env_fallbacks(dict(DEFAULT_CONFIG))


def save(config: dict) -> bool:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged = {**DEFAULT_CONFIG, **_without_removed_keys(config)}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[Config] save error: {e}")
        return False
