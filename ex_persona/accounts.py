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
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32

# 常见弱密码：命中即拒绝（小写比较）。
WEAK_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "passw0rd",
    "123456789",
    "1234567890",
    "qwertyuiop",
    "qwerty123",
    "1qaz2wsx",
    "letmein",
    "iloveyou",
    "welcome1",
    "admin123",
    "admin888",
    "abc12345",
}

# 登录失败锁定：窗口内达到阈值即锁定。
LOGIN_FAILURE_WINDOW_SECONDS = 900
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 900

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


def _password_problems(password: str, username: str = "") -> list[str]:
    """收集密码不满足强度的原因，空列表表示通过。"""
    value = password or ""
    problems: list[str] = []
    if len(value) < PASSWORD_MIN_LENGTH:
        problems.append(f"密码至少 {PASSWORD_MIN_LENGTH} 个字符")
    elif len(value) > PASSWORD_MAX_LENGTH:
        problems.append(f"密码最多 {PASSWORD_MAX_LENGTH} 个字符")
    missing: list[str] = []
    if not any(char.isupper() for char in value):
        missing.append("大写字母")
    if not any(char.islower() for char in value):
        missing.append("小写字母")
    if not any(char.isdigit() for char in value):
        missing.append("数字")
    if not any((not char.isalnum()) and (not char.isspace()) for char in value):
        missing.append("符号")
    if missing:
        problems.append("密码需包含" + "、".join(missing))
    lowered = value.lower()
    name = (username or "").strip().lower()
    if lowered in WEAK_PASSWORDS:
        problems.append("密码过于简单，请勿使用常见弱密码")
    elif len(name) >= 3 and name in lowered:
        problems.append("密码不能包含用户名")
    return problems


def validate_password_strength(password: str, username: str = "") -> None:
    """统一密码强度校验：长度、四类字符、弱密码与用户名相似。"""
    problems = _password_problems(password, username)
    if problems:
        raise AccountError(problems[0])


def check_password_strength(password: str, username: str = "") -> dict:
    """供前端实时提示：返回是否通过以及全部原因。"""
    problems = _password_problems(password, username)
    return {"ok": not problems, "problems": problems}


def validate_credentials(username: str, password: str) -> None:
    username = (username or "").strip()
    if len(username) < MIN_USERNAME:
        raise AccountError(f"用户名至少 {MIN_USERNAME} 个字符")
    if any(char.isspace() for char in username):
        raise AccountError("用户名不能包含空白字符")
    validate_password_strength(password, username)


def register(username: str, password: str) -> dict:
    username = (username or "").strip()
    validate_credentials(username, password)
    if store.get_user_by_username(username) is not None:
        raise AccountError("该用户名已被占用", status_code=409)
    salt, digest = hash_password(password)
    return store.create_user(username=username, password_hash=digest, password_salt=salt)


def _locked_error(state: dict) -> AccountError:
    remaining = max(int(state.get("remaining_seconds") or 0), 1)
    minutes = max(1, (remaining + 59) // 60)
    error = AccountError(f"账户已临时锁定，请在 {minutes} 分钟后重试", status_code=429)
    error.retry_after = remaining  # type: ignore[attr-defined]
    return error


def _record_failure(kind: str, account_id: int) -> None:
    state = store.record_login_failure(
        kind,
        account_id,
        LOGIN_FAILURE_WINDOW_SECONDS,
        LOGIN_MAX_FAILURES,
        LOGIN_LOCK_SECONDS,
    )
    if state["locked"]:
        raise _locked_error(state)


def authenticate(username: str, password: str) -> dict:
    user = store.get_user_by_username((username or "").strip())
    if user is not None:
        state = store.get_lock_state("user", user["id"])
        if state["locked"]:
            raise _locked_error(state)
    if user is None or not verify_password(password or "", user.get("password_salt"), user.get("password_hash")):
        if user is not None:
            _record_failure("user", user["id"])
        raise AccountError("用户名或密码错误", status_code=401)
    store.clear_login_failures("user", user["id"])
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
    validate_password_strength(new, user.get("username") or "")
    if user.get("password_hash") and verify_password(new or "", user.get("password_salt"), user.get("password_hash")):
        raise AccountError("新密码不能与当前密码相同")
    salt, digest = hash_password(new)
    store.set_user_username(user_id, user.get("username") or f"user{user_id}", digest, salt)
    store.mark_password_changed(user_id)


def set_username_password(user_id: int, username: str, password: str) -> None:
    username = (username or "").strip()
    validate_credentials(username, password)
    existing = store.get_user_by_username(username)
    if existing is not None and existing["id"] != user_id:
        raise AccountError("该用户名已被占用", status_code=409)
    salt, digest = hash_password(password)
    store.set_user_username(user_id, username, digest, salt)


# --------------------------------------------------------------------------- #
# 管理员账号（与用户账号完全分离）
# --------------------------------------------------------------------------- #


def create_admin(username: str, password: str, name: str = "") -> dict:
    username = (username or "").strip()
    validate_credentials(username, password)
    if store.get_admin_by_username(username) is not None:
        raise AccountError("该管理员用户名已存在", status_code=409)
    salt, digest = hash_password(password)
    return store.create_admin(username, digest, salt, name=(name or "").strip())


def authenticate_admin(username: str, password: str) -> dict:
    admin = store.get_admin_by_username((username or "").strip())
    if admin is not None:
        state = store.get_lock_state("admin", admin["id"])
        if state["locked"]:
            raise _locked_error(state)
    if admin is None or not verify_password(
        password or "", admin.get("password_salt"), admin.get("password_hash")
    ):
        if admin is not None:
            _record_failure("admin", admin["id"])
        raise AccountError("管理员用户名或密码错误", status_code=401)
    store.clear_login_failures("admin", admin["id"])
    if (admin.get("status") or "active") != "active":
        raise AccountError("管理员账号已被停用", status_code=403)
    return admin


def start_admin_session(admin_id: int, days: int = 7, pending_totp: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    store.create_admin_session(admin_id, token, days=days, pending_totp=pending_totp)
    return token


def admin_session(token: str | None, allow_pending: bool = False) -> dict | None:
    admin = store.get_admin_by_session(token)
    if admin is None:
        return None
    if admin.get("totp_pending") and not allow_pending:
        return None
    return admin


def end_admin_session(token: str | None) -> None:
    store.delete_admin_session(token)


def set_admin_password(admin_id: int, new: str) -> None:
    admin = store.get_admin(admin_id)
    validate_password_strength(new, (admin or {}).get("username") or "")
    salt, digest = hash_password(new)
    store.set_admin_credentials(admin_id, digest, salt)
