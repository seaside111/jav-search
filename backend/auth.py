"""
简单登录认证 — 账号密码来自环境变量，Cookie 会话用 HMAC 签名 token。
不依赖额外库，使用标准库 hmac / hashlib / time。
"""
import os
import hmac
import hashlib
import time
import secrets
import base64

# 从环境变量读取凭据
AUTH_USERNAME = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")
# 是否启用认证：密码为空则关闭（方便首次调试）
AUTH_ENABLED = bool(AUTH_PASSWORD)
# 会话有效期（秒），默认 7 天
SESSION_TTL = int(os.getenv("AUTH_SESSION_TTL", str(7 * 24 * 3600)))
# 签名密钥：优先用环境变量，否则进程启动时随机生成（重启后旧会话失效）
SECRET = os.getenv("AUTH_SECRET", "") or secrets.token_hex(32)

COOKIE_NAME = "jav_session"


def verify_credentials(username: str, password: str) -> bool:
    """校验账号密码（恒定时间比较，防时序攻击）"""
    if not AUTH_ENABLED:
        return True
    u_ok = hmac.compare_digest(username or "", AUTH_USERNAME)
    p_ok = hmac.compare_digest(password or "", AUTH_PASSWORD)
    return u_ok and p_ok


def create_token() -> str:
    """生成签名 token：base64(expire_ts).signature"""
    expire = int(time.time()) + SESSION_TTL
    payload = str(expire).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = _sign(payload_b64)
    return f"{payload_b64}.{sig}"


def _sign(data: str) -> str:
    mac = hmac.new(SECRET.encode(), data.encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def verify_token(token: str) -> bool:
    """校验 token 签名与有效期"""
    if not AUTH_ENABLED:
        return True
    if not token or "." not in token:
        return False
    try:
        payload_b64, sig = token.rsplit(".", 1)
        # 验签
        expected = _sign(payload_b64)
        if not hmac.compare_digest(sig, expected):
            return False
        # 验有效期
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        expire = int(base64.urlsafe_b64decode(padded).decode())
        return time.time() < expire
    except Exception:
        return False
