"""平台主密钥与敏感字段加解密。

用户的大模型 API Key 以 Fernet 对称加密后入库，明文不出现在数据库、
日志与 HTTP 响应中。主密钥优先级：

1. 环境变量 ``PERSONA_SECRET_KEY``
2. 数据目录下的 ``.secret_key``（首次运行自动生成，权限 0600）
"""

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception

_SECRET_FILE = ".secret_key"


def data_root() -> Path:
    return Path(os.getenv("PERSONA_DATA_DIR", "data"))


def _load_or_create_secret() -> bytes:
    env = (os.getenv("PERSONA_SECRET_KEY") or "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    path = data_root() / _SECRET_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(32))
        os.chmod(path, 0o600)
    return path.read_bytes()


def _cipher():
    if Fernet is None:
        raise RuntimeError("未安装 cryptography，请执行 pip install -r requirements.txt")
    key = base64.urlsafe_b64encode(_load_or_create_secret())
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return "*" * 8 + secret[-4:]


def _bridge_key() -> bytes:
    return hashlib.sha256(b"bridge:" + _load_or_create_secret()).digest()


def new_bridge_token(user_id: int) -> str:
    """生成 ``user_id.nonce.signature`` 形式的桥接令牌，签名与平台主密钥绑定。"""
    nonce = secrets.token_urlsafe(24)
    payload = f"{user_id}.{nonce}"
    signature = hmac.new(
        _bridge_key(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"{payload}.{signature}"


def verify_bridge_token(token: str) -> bool:
    """校验签名令牌。旧版无签名的令牌返回 False，由调用方按兼容策略处理。"""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return False
    payload = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(
        _bridge_key(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, parts[2])


def signed_bridge_token(token: str) -> bool:
    """判断令牌是否为签名格式。"""
    return (token or "").count(".") == 2
