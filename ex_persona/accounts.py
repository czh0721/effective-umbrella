"""账号与会话：用户名密码注册登录、scrypt 密码哈希、会话签发与校验。"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

from . import store

MIN_USERNAME = 3
MIN_PASSWORD = 8
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32

# TOTP（RFC 6238）：SHA1、6 位、30 秒步长，兼容主流验证器 App。
TOTP_DIGITS = 6
TOTP_STEP = 30
TOTP_WINDOW = 1


class AccountError(Exception):
    """账号相关的可预期错误，携带面向用户的提示。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN
    )


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    return salt.hex(), _derive(password, salt).hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex or "")
        expected = bytes.fromhex(hash_hex or "")
    except ValueError:
        return False
    if not salt or not expected:
        return False
    return hmac.compare_digest(_derive(password, salt), expected)


def validate_credentials(username: str, password: str) -> None:
    username = (username or "").strip()
    if len(username) < MIN_USERNAME:
        raise AccountError(f"用户名至少 {MIN_USERNAME} 个字符")
    if any(char.isspace() for char in username):
        raise AccountError("用户名不能包含空白字符")
    if len(password or "") < MIN_PASSWORD:
        raise AccountError(f"密码至少 {MIN_PASSWORD} 个字符")


def register(username: str, password: str) -> dict:
    username = (username or "").strip()
    validate_credentials(username, password)
    if store.get_user_by_username(username) is not None:
        raise AccountError("该用户名已被占用", status_code=409)
    salt, digest = hash_password(password)
    return store.create_user(username=username, password_hash=digest, password_salt=salt)


def authenticate(username: str, password: str) -> dict:
    user = store.get_user_by_username((username or "").strip())
    if user is None or not verify_password(password or "", user.get("password_salt"), user.get("password_hash")):
        raise AccountError("用户名或密码错误", status_code=401)
    if (user.get("status") or "active") != "active":
        raise AccountError("账号已被停用，请联系管理员", status_code=403)
    return user


def start_session(user_id: int, days: int = 7, pending_totp: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    store.create_session(user_id, token, days=days, pending_totp=pending_totp)
    return token


def session_user(token: str | None, allow_pending: bool = False) -> dict | None:
    user = store.get_user_by_session(token)
    if user is None:
        return None
    if user.get("totp_pending") and not allow_pending:
        return None
    if (user.get("status") or "active") != "active":
        return None
    return user


def end_session(token: str | None) -> None:
    store.delete_session(token)


# --------------------------------------------------------------------------- #
# TOTP 二次验证
# --------------------------------------------------------------------------- #


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret: str, counter: int) -> str:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + padding)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % (10 ** TOTP_DIGITS):0{TOTP_DIGITS}d}"


def totp_code_now(secret: str, at: float | None = None) -> str:
    """按当前时间生成验证码，供测试与服务端校验复用。"""
    moment = time.time() if at is None else at
    return _totp_code(secret, int(moment) // TOTP_STEP)


def verify_totp(secret: str, code: str, at: float | None = None) -> bool:
    """校验验证码，允许前后各 ``TOTP_WINDOW`` 个步长以容忍时钟漂移。"""
    if not secret:
        return False
    candidate = (code or "").strip().replace(" ", "")
    if len(candidate) != TOTP_DIGITS or not candidate.isdigit():
        return False
    moment = time.time() if at is None else at
    counter = int(moment) // TOTP_STEP
    for drift in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        if hmac.compare_digest(_totp_code(secret, counter + drift), candidate):
            return True
    return False


def totp_uri(secret: str, username: str, issuer: str = "念念") -> str:
    label = f"{issuer}:{username or 'user'}"
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}"
    )


def change_password(user_id: int, current: str, new: str) -> None:
    user = store.get_user(user_id)
    if user is None:
        raise AccountError("账号不存在", status_code=404)
    if user.get("password_hash") and not verify_password(current or "", user.get("password_salt"), user.get("password_hash")):
        raise AccountError("当前密码不正确", status_code=401)
    if len(new or "") < MIN_PASSWORD:
        raise AccountError(f"密码至少 {MIN_PASSWORD} 个字符")
    if user.get("password_hash") and verify_password(new or "", user.get("password_salt"), user.get("password_hash")):
        raise AccountError("新密码不能与当前密码相同")
    salt, digest = hash_password(new)
    store.set_user_username(user_id, user.get("username") or f"user{user_id}", digest, salt)


def set_username_password(user_id: int, username: str, password: str) -> None:
    username = (username or "").strip()
    validate_credentials(username, password)
    existing = store.get_user_by_username(username)
    if existing is not None and existing["id"] != user_id:
        raise AccountError("该用户名已被占用", status_code=409)
    salt, digest = hash_password(password)
    store.set_user_username(user_id, username, digest, salt)
