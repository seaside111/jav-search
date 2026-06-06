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
        "fc2": 30,                       # FC2 走 FlareSolverr 较慢，数量不宜过大
    },
    # Jackett
    "jackett_url": "",                   # Jackett 地址，如 http://192.168.1.100:9117
    "jackett_api_key": "",               # Jackett API Key
    "jackett_indexers": "all",           # 索引器，多个用逗号分隔
    "jackett_timeout": 20,               # 搜索超时秒数
    # V1.4：qBittorrent 下载器（群晖中部署）
    "qb_url": "",                        # qBittorrent WebUI 地址，如 http://192.168.1.100:8080
    "qb_username": "",                   # WebUI 用户名
    "qb_password": "",                   # WebUI 密码
    "qb_save_path": "",                  # 推送任务的保存目录（qB 主机视角），留空用 qB 默认
    "qb_category": "jav",                # 任务分类，便于刮削监控筛选；留空不分类
    "qb_paused": False,                  # 推送后是否暂停（先不下载）
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
