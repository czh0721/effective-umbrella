"""多用户平台后端：账号、人格、素材、模型配置、微信绑定与消息路由。"""

import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    accounts,
    clock,
    credits,
    crypto,
    distill,
    humanize,
    ingest,
    media_reply,
    memories,
    moments,
    observability,
    outbox,
    persona_files,
    persona_settings,
    presets,
    safety,
    scheduler,
    store,
    wechat,
    wechat_login,
    workspace,
)
from .agent import PersonaAgent
from .config import LLMConfig, load_platform_config
from .llm import LLMError

log = logging.getLogger("nian.webapp")

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
SESSION_COOKIE = "persona_session"
PORT = int(os.getenv("PERSONA_PORT", os.getenv("PORT", "8000")))
MAX_FILES = 20
MAX_FILE_BYTES = 50 * 1024 * 1024
DEDUP_SECONDS = 4
# 相同消息的最短去重窗口：微信重复推送时直接复用上一次回复，不再落库。
DEDUP_MIN_SECONDS = 30
# 每个桥接令牌每分钟允许的最大请求数，防止令牌泄露后被刷。
RATE_LIMIT_PER_MINUTE = 90
# weclaw 进程健康检查间隔（秒）。
WATCHDOG_SECONDS = 30
# weclaw 转发图片时使用的占位文本；是否回应由人格设置 reply_to_images 决定。
IMAGE_PLACEHOLDER = "[图片]"
# 单用户最多可创建的人格的 Agent 数量，防止刷资源。
MAX_PERSONAS_PER_USER = 20
# 单次积分/念念币调整与发放的上限，避免超大整数溢出或误操作。
MAX_GRANT_DELTA = 100_000_000
# 同一来源 IP 在防爆破时间窗内最多成功注册的账号数（0 表示不限）。
REGISTER_MAX_PER_WINDOW = int(os.environ.get("PERSONA_REGISTER_MAX", "5") or 5)
# 单条出站消息的最大字符数，以及需要剔除的控制字符。
MAX_REPLY_CHARS = 2000
# 失败态对用户可见的兜底话术：平台不可用/模型出错/额度用尽/积分不足时，
# 直接以正常回复形式发出，避免 weclaw 因非 200 响应而静默丢弃。
REPLY_UNAVAILABLE = "我这边信号不太好，一会儿再回你。"
REPLY_MODEL_ERROR = "我这会儿有点累，稍后再来找我好不好。"
REPLY_PLATFORM_CAPPED = "今天聊得有点多啦，明天再来找我吧。"
REPLY_NO_CREDITS = "我的积分用完了，暂时回不了你。去念念「设置」里兑换念念币就能继续聊。"
# 记忆单条内容长度上限。
MAX_MEMORY_CHARS = 500
# 长期记忆召回时最多扫描的条数（按时间倒序），避免超长记忆表拖慢回复。
MEMORY_SCAN_LIMIT = 300
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_outbound(text: str) -> str:
    """发送前的轻量清洗：去掉控制字符、统一换行并限制长度。"""
    cleaned = _CONTROL_RE.sub("", text or "").replace("\r\n", "\n").strip()
    if len(cleaned) > MAX_REPLY_CHARS:
        cleaned = cleaned[:MAX_REPLY_CHARS].rstrip()
    return cleaned


# --------------------------------------------------------------------------- #
# 防封策略：敏感词、拟人延迟、发送配额与站内提醒
# --------------------------------------------------------------------------- #

# 同一种提醒的冷却时间（秒），避免掉线时反复打扰用户。
ALERT_COOLDOWN_SECONDS = 1800
_alert_at: dict[tuple[int, str], float] = {}
_alert_lock = threading.Lock()


def _safety(settings: dict) -> dict:
    return (settings or {}).get("safety") or {}


def _wechat_online() -> bool:
    """只有配置了 weclaw 才有真实发送通道，才需要模拟打字延迟。"""
    return bool(wechat.WECLAW_BIN)


def _day_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _quota_ok(user_id: int, settings: dict) -> bool:
    """检查是否还在每分钟/每日发送配额内；配额的 0 表示不限制。"""
    rules = _safety(settings)
    if not rules.get("enabled", True):
        return True
    try:
        per_minute = int(rules.get("per_minute") or 0)
        daily = int(rules.get("daily_limit") or 0)
    except (TypeError, ValueError):
        return True
    if per_minute:
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        if store.count_send_events_since(user_id, since) >= per_minute:
            return False
    if daily and store.count_send_events_since(user_id, _day_start_iso()) >= daily:
        return False
    return True


def _record_send(user_id: int) -> None:
    try:
        store.add_send_event(user_id)
    except Exception:  # noqa: BLE001 - 统计失败不影响发送
        log.exception("record send failed", extra={"event": "safety.record_error"})


def _notify(user_id: int, kind: str, message: str) -> None:
    """写入站内提醒；同类提醒在冷却期内只记一次。"""
    key = (int(user_id), kind)
    now = time.time()
    with _alert_lock:
        if now - _alert_at.get(key, 0.0) < ALERT_COOLDOWN_SECONDS:
            return
        _alert_at[key] = now
    try:
        store.add_alert(user_id, kind, message)
    except Exception:  # noqa: BLE001 - 提醒失败不影响主流程
        log.exception("add alert failed", extra={"event": "safety.alert_error"})


def _apply_filter(text: str, settings: dict) -> str:
    """按人格设置过滤敏感词；关闭或未命中时原样返回。"""
    rules = _safety(settings)
    if not text or not rules.get("enabled", True) or not rules.get("sensitive_filter", True):
        return text
    cleaned, hit = safety.scrub(text, action=rules.get("sensitive_action", "replace"))
    if hit:
        observability.METRICS.inc("reply.sensitive")
    return cleaned


def _with_tuning(settings: dict, extra_context: str) -> str:
    """把人格调校提示并入额外上下文，随每次回复下发给模型。"""
    hint = persona_settings.tuning_hint(settings)
    if not hint:
        return extra_context
    return f"{extra_context}\n\n{hint}".strip() if extra_context else hint


def _reply_delay_seconds(reply: str, settings: dict) -> float:
    """按拟人节奏估算首条回复的等待秒数；不需要延迟时返回 0。"""
    rules = _safety(settings)
    if not _wechat_online() or not rules.get("enabled", True) or not rules.get("reply_delay", True):
        return 0.0
    seconds = safety.reply_delay(
        reply,
        minimum=float(rules.get("delay_min", 0.8)),
        maximum=float(rules.get("delay_max", 4.0)),
    )
    return float(seconds or 0.0)


def _human_delay(reply: str, settings: dict) -> None:
    """回复前按拟人节奏随机等待，模拟真人打字。"""
    seconds = _reply_delay_seconds(reply, settings)
    if seconds > 0:
        observability.METRICS.inc("reply.delayed")
        time.sleep(seconds)


# 允许的图片类型：按文件头识别，避免把任意文件（含 HTML）当成表情包存储与回源。
_IMAGE_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def _save_upload_image(data: bytes, filename: str, directory: Path) -> tuple[Path, str]:
    """校验并保存图片，返回 (磁盘路径, 展示名)。非图片直接拒绝。"""
    mime = _sniff_image_mime(data)
    if not mime:
        raise HTTPException(status_code=400, detail="只支持图片文件（PNG/JPG/GIF/WEBP/BMP）")
    directory.mkdir(parents=True, exist_ok=True)
    display = workspace.safe_filename(filename or "image")
    stem = Path(display).stem[:40] or "image"
    target = workspace.safe_join(directory, f"{uuid.uuid4().hex[:8]}-{stem}{_IMAGE_SUFFIX[mime]}")
    target.write_bytes(data)
    return target, display

app = FastAPI(title="念念 Nian", version="0.2.0")
(WEB_DIR / "static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """统一安全响应头，作为 Nginx 之外的应用层兜底。"""
    if _csrf_violation(request):
        return JSONResponse({"detail": "请求来源校验失败，请刷新页面后重试"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # HSTS：仅在 HTTPS 下生效；不包含 includeSubDomains，避免影响同根域的其他站点。
    if _is_https_request(request):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; preload"
        )
    return response


def _is_https_request(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    url = request.url
    return url.scheme == "https"


# 会改变状态的请求方法；对这些请求做跨站来源校验（CSRF 兜底）。
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 桥接接口由本机 weclaw 进程调用，不依赖浏览器 Cookie，无需 CSRF 校验。
_CSRF_EXEMPT_PREFIXES = ("/v1/",)


def _allowed_origin_hosts(request: Request) -> set[str]:
    """可信来源主机：当前请求 Host + 环境变量 PERSONA_ALLOWED_ORIGINS 列表。"""
    hosts: set[str] = set()
    header_host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if header_host:
        hosts.add(header_host)
    for item in (os.getenv("PERSONA_ALLOWED_ORIGINS") or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        parsed = urllib.parse.urlsplit(item if "//" in item else f"//{item}")
        host = (parsed.hostname or "").lower()
        if host:
            hosts.add(host)
    return hosts


def _csrf_violation(request: Request) -> bool:
    """判断跨站写请求是否应被拒绝。

    优先看浏览器自带的 ``Sec-Fetch-Site``，缺失时回退比较 Origin/Referer 主机。
    没有任何来源信息时（CLI、测试、本机调用）放行，保持非浏览器客户端可用。
    """
    if request.method.upper() not in _CSRF_UNSAFE_METHODS:
        return False
    path = request.url.path
    if path.startswith(_CSRF_EXEMPT_PREFIXES):
        return False
    if not (path.startswith("/api/") or path.startswith("/admin")):
        return False
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site == "cross-site":
        return True
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        origin = (request.headers.get("referer") or "").strip()
    if not origin:
        return False
    parsed = urllib.parse.urlsplit(origin)
    host = (parsed.hostname or "").lower()
    if not host:
        return True
    return host not in _allowed_origin_hosts(request)


def _validate_base_url(raw: str) -> str:
    """校验用户自定义模型端点，避免把服务端出站请求指向内网（SSRF）。"""
    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="接口地址不能为空")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="接口地址必须是合法的 http(s) 地址")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise HTTPException(status_code=400, detail="接口地址不允许指向内网")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return value
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise HTTPException(status_code=400, detail="接口地址不允许指向内网")
    return value


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：记录结构化日志，对外只返回通用错误。"""
    log.exception(
        "unhandled error",
        extra={"event": "http.unhandled", "error": type(exc).__name__},
    )
    return JSONResponse({"detail": "服务器内部错误，请稍后重试"}, status_code=500)

def _on_bridge_event(user_id: int, event: str) -> None:
    """桥接状态变化落库；会话失效时额外提醒用户重新扫码。"""
    try:
        store.set_binding_phase(user_id, event)
    except Exception:  # noqa: BLE001 - 状态落库失败不影响桥接
        pass
    if event == "expired":
        try:
            _notify(user_id, "offline", "微信登录已失效，请到「消息渠道」重新扫码以恢复收发。")
        except Exception:  # noqa: BLE001
            pass


manager = wechat.WeChatManager(PORT, on_event=_on_bridge_event)
_agents: dict[str, tuple[str, PersonaAgent]] = {}
_last_batch: dict[int, Path] = {}
_reply_cache: dict[tuple[int, str, str], tuple[float, str]] = {}
_reply_locks: dict[tuple[int, str, str], threading.Lock] = {}
_reply_locks_guard = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
_scheduler: scheduler.ProactiveScheduler | None = None
_moments_scheduler: moments.MomentScheduler | None = None
_credit_worker: credits.CreditExpiryWorker | None = None
_memory_locks: dict[tuple[int, int, str], threading.Lock] = {}
_memory_locks_lock = threading.Lock()

# 登录/注册防爆破：按来源 IP 与用户名分别计数，超限后短暂拒绝，避免撞库与 scrypt 资源耗尽。
AUTH_WINDOW_SECONDS = 300
AUTH_MAX_ATTEMPTS = 10
_auth_attempts: dict[str, list[float]] = {}
_auth_attempts_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _auth_blocked(*keys: str) -> bool:
    now = time.time()
    with _auth_attempts_lock:
        for key in keys:
            bucket = _auth_attempts.get(key)
            if bucket:
                bucket[:] = [stamp for stamp in bucket if now - stamp < AUTH_WINDOW_SECONDS]
                if len(bucket) >= AUTH_MAX_ATTEMPTS:
                    return True
    return False


def _auth_note(*keys: str) -> None:
    now = time.time()
    with _auth_attempts_lock:
        for key in keys:
            _auth_attempts.setdefault(key, []).append(now)
        if len(_auth_attempts) > 5000:
            for old in list(_auth_attempts)[:2500]:
                _auth_attempts.pop(old, None)


def _auth_clear(*keys: str) -> None:
    with _auth_attempts_lock:
        for key in keys:
            _auth_attempts.pop(key, None)


# 注册限流：同一来源 IP 在时间窗内最多成功注册 REGISTER_MAX_PER_WINDOW 个账号，
# 抑制批量注册；与登录失败计数分开，避免正常登录消耗注册额度。
_register_attempts: dict[str, list[float]] = {}


def _register_blocked(ip: str) -> bool:
    if REGISTER_MAX_PER_WINDOW <= 0:
        return False
    now = time.time()
    with _auth_attempts_lock:
        bucket = _register_attempts.get(ip, [])
        bucket[:] = [stamp for stamp in bucket if now - stamp < AUTH_WINDOW_SECONDS]
        return len(bucket) >= REGISTER_MAX_PER_WINDOW


def _register_note(ip: str) -> None:
    now = time.time()
    with _auth_attempts_lock:
        _register_attempts.setdefault(ip, []).append(now)
        if len(_register_attempts) > 5000:
            for old in list(_register_attempts)[:2500]:
                _register_attempts.pop(old, None)


def _new_task(user_id: int, persona_id: int, kind: str, client_id: str = "",
              task_id: str = "") -> str:
    task_id = task_id or uuid.uuid4().hex
    store.create_task(task_id, user_id, persona_id, kind, client_id=client_id)
    return task_id


def _update_task(task_id: str, **fields) -> None:
    if "step" in fields:
        fields["message"] = fields.pop("step")
    store.update_task(task_id, **fields)


def _reserve_distill_ticket(user_id: int, task_id: str) -> None:
    """发起蒸馏前预扣 1 张蒸馏券；券不足时返回 402。"""
    try:
        store.reserve_distill_ticket(user_id, task_id)
    except store.InsufficientDistillTickets as error:
        raise HTTPException(
            status_code=402, detail=f"蒸馏券不足，请先购买蒸馏券（当前 {error.balance} 张）"
        ) from error


def _refund_distill_ticket(user_id: int, task_id: str, reason: str = "") -> None:
    """蒸馏失败/取消时幂等退还预扣的蒸馏券。"""
    try:
        store.refund_distill_ticket(
            user_id, task_id, reason or store.TICKET_REFUND_REASON
        )
    except Exception as error:  # noqa: BLE001 - 退款失败只记录，不影响主流程
        log.warning(
            "refund distill ticket failed",
            extra={"event": "distill.ticket_refund_error", "user_id": user_id,
                   "task_id": task_id, "error": str(error)},
        )


def _task_payload(task: dict) -> dict:
    payload = dict(task)
    payload["step"] = payload.pop("message", "")
    payload.pop("user_id", None)
    return payload


def get_scheduler() -> scheduler.ProactiveScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = scheduler.ProactiveScheduler(
            _agent_factory, _proactive_send, check_seconds=60
        )
    return _scheduler


def _moment_charge(user_id: int, persona_id: int) -> int:
    """自动发布前置扣费；仅平台内置模型计费，未启用或自带 Key 时不扣减。"""
    persona = store.get_persona(user_id, persona_id)
    if persona is None:
        return 0
    try:
        agent = get_agent(user_id, persona)
    except Exception:  # noqa: BLE001 - 人格数据缺失时按免费处理
        return 0
    cost = _moment_cost_for(agent)
    if cost <= 0:
        return 0
    store.deduct_credits(
        user_id, cost, reason="朋友圈发布扣费", ref=f"persona:{persona_id}"
    )
    return cost


def _moment_refund(user_id: int, amount: int, persona_id: int) -> None:
    amount = int(amount or 0)
    if amount <= 0:
        return
    try:
        store.grant_credits(
            user_id, amount, reason="朋友圈发布失败退款", actor="system",
            ref=f"persona:{persona_id}",
        )
    except Exception:  # noqa: BLE001 - 退款失败不阻断调度
        log.warning("moment refund failed", extra={"event": "moments.refund_fail"})


def get_moments_scheduler() -> moments.MomentScheduler:
    global _moments_scheduler
    if _moments_scheduler is None:
        _moments_scheduler = moments.MomentScheduler(
            _agent_factory,
            _moment_charge,
            _moment_refund,
            _notify,
            check_seconds=300,
        )
    return _moments_scheduler


def get_credit_worker() -> credits.CreditExpiryWorker:
    global _credit_worker
    if _credit_worker is None:
        _credit_worker = credits.CreditExpiryWorker()
    return _credit_worker


def _wants_forwarding(binding: dict) -> bool:
    """用户是否希望转发处于开启状态（running / 曾启动失败需重试）。

    用户手动关掉的渠道（logged-in）与登录失效的渠道（expired）返回 False，
    这样服务重启、打开渠道面板都不会把它们重新打开。
    """
    return (binding.get("phase") or "") in ("running", "failed")


def _autostart_bridges() -> None:
    """为已绑定微信的用户恢复转发；单用户失败不影响其他用户。"""
    for binding in store.list_wechat_bindings():
        token = binding.get("bridge_token")
        home = binding.get("home_dir")
        if not token or not home:
            continue
        phase = binding.get("phase") or ""
        if phase not in ("running", "failed", "expired"):
            continue
        try:
            bridge = manager.get(binding["user_id"], Path(home), token)
            if phase == "expired":
                # 会话失效多为历史扫码残留账号污染所致；只保留最新账号后再试一次。
                if len(bridge.account_files()) > 1:
                    bridge.recover()
                continue
            bridge.maybe_autostart()
        except Exception:  # noqa: BLE001 - 单个用户失败不影响其他用户
            continue


def _check_bridges() -> None:
    """检查已登录过的用户是否掉线，必要时写入站内提醒。"""
    for binding in store.list_wechat_bindings():
        home = binding.get("home_dir")
        if not home or not wechat.bound_accounts(home):
            continue
        # 只在“本应在线”的用户掉线时提醒，避免打扰只是绑定但未启用转发的用户。
        if (binding.get("phase") or "") != "running":
            continue
        user_id = int(binding["user_id"])
        try:
            if manager.is_running(user_id):
                continue
        except Exception:  # noqa: BLE001 - 单个用户异常不影响其他用户
            pass
        persona = store.get_active_persona(user_id)
        settings = persona_settings.load(persona.get("settings")) if persona else {}
        if not _safety(settings).get("offline_alert", True):
            continue
        _notify(user_id, "offline", "微信连接已断开，请到「消息渠道」重新扫码以恢复收发。")


def _bridge_watchdog() -> None:
    """周期性健康检查：weclaw 进程若意外退出则自动拉起。"""
    while True:
        try:
            _autostart_bridges()
            _check_bridges()
        except Exception:  # noqa: BLE001 - 守护线程永不退出
            pass
        time.sleep(WATCHDOG_SECONDS)


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    observability.setup_logging()
    store.prune_tasks()
    store.fail_stale_tasks()
    store.reconcile_distill_tickets()
    store.purge_expired_sessions()
    store.purge_expired_login_states()
    _apply_admin_whitelist()
    get_scheduler().start()
    get_moments_scheduler().start()
    get_credit_worker().start()
    _outbox.start()
    if wechat.WECLAW_BIN:
        threading.Thread(target=_bridge_watchdog, daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if _scheduler is not None:
        _scheduler.stop()
    if _moments_scheduler is not None:
        _moments_scheduler.stop()
    if _credit_worker is not None:
        _credit_worker.stop()
    _outbox.stop()
    manager.stop_all()


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #

def _secure_cookie(request: Request | None = None) -> bool:
    if (os.getenv("PERSONA_SECURE_COOKIE") or "").strip() == "1":
        return True
    return bool(request is not None and _is_https_request(request))


def _public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user.get("username"),
        "nickname": user.get("nickname"),
        "avatar": user.get("avatar"),
        "role": user.get("role", "user"),
        "created_at": user.get("created_at"),
    }


def current_user(request: Request) -> dict:
    user = accounts.session_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _login_response(user: dict, request: Request | None = None, *, pending_totp: bool = False) -> JSONResponse:
    token = accounts.start_session(user["id"], pending_totp=pending_totp)
    body = {"ok": True, "totp_required": True} if pending_totp else {"ok": True, "user": _public_user(user)}
    response = JSONResponse(body)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )
    return response


def _page(file_name: str):
    def handler(request: Request):
        user = accounts.session_user(request.cookies.get(SESSION_COOKIE))
        if user is None:
            return RedirectResponse("/login", status_code=302)
        return FileResponse(WEB_DIR / file_name)

    return handler


# --------------------------------------------------------------------------- #
# 模型配置与 Agent
# --------------------------------------------------------------------------- #

def own_key_usable(row: dict | None) -> bool:
    """用户自带 Key 是否可用；未配置或已被标记失效都视为不可用。"""
    if not row or not row.get("api_key_encrypted"):
        return False
    return (row.get("key_status") or "") != "invalid"


def history_for_llm(stored: list[dict], settings: dict) -> list[dict]:
    """组装给模型的历史。

    兜底话术（例如「晚点回你」）不是分身的真实回复。它一旦留在历史里，模型会
    把它当成范例反复复读；而只删助手那一半，又会留下「一连串没有回应的用户
    消息」，模型接不上话。因此按「一问一答」成对保留：只有拿到真实回复的用户
    消息才进入历史，落单的用户消息和兜底回复一起丢弃。
    """
    fallback = str(settings["model"].get("fallback_reply") or "").strip()

    def _is_fallback(item: dict) -> bool:
        return (
            item.get("role") == "assistant"
            and bool(fallback)
            and (item.get("content") or "").strip() == fallback
        )

    history: list[dict] = []
    pending: dict | None = None
    for item in stored:
        role = item.get("role")
        if role == "user":
            # 上一条用户消息还没等到真实回复，说明那次对话没成立，一并丢弃。
            pending = item
            continue
        if role != "assistant":
            continue
        if _is_fallback(item):
            pending = None
            continue
        if pending is not None:
            history.append({"role": "user", "content": pending.get("content")})
            pending = None
        history.append({"role": "assistant", "content": item.get("content")})
    if pending is not None:
        # 最后一条用户消息还没被回复（可能刚失败），保留它让模型知道对方说了什么。
        history.append({"role": "user", "content": pending.get("content")})
    return history


def build_config(user_id: int, persona: dict | None = None) -> LLMConfig:
    row = store.get_model_config(user_id)
    api_key = crypto.decrypt(row.get("api_key_encrypted")) if row else ""
    # 旧版「自带 Key」入口已下线，历史遗留且已被标记失效的 Key 不该再拦截平台内置
    # 模型，否则用户每条消息都会拿到人格的兜底回复。
    if api_key and not own_key_usable(row):
        api_key = ""
    base_url = (row.get("base_url") if row else None) or "https://api.deepseek.com/v1"
    model = (row.get("model") if row else None) or "deepseek-chat"
    fallback_reply = ""
    provider = "system"
    if persona is not None:
        settings = persona_settings.load(persona.get("settings"))
        provider = settings["model"].get("provider") or "system"
        base_url, model = persona_settings.provider_model(provider, base_url, model)
        fallback_reply = str(settings["model"].get("fallback_reply") or "")
    # 用户未配置自备 Key 时，无论选择哪个厂商都回退到平台内置模型，
    # 避免「选择厂商但无 Key」导致的空 Key 调用失败。
    if not api_key:
        platform = load_platform_config()
        if platform.ready:
            return LLMConfig(
                api_key=platform.api_key,
                base_url=platform.base_url,
                model=platform.model,
                fallback_reply=fallback_reply,
                platform=True,
            )
    return LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        fallback_reply=fallback_reply,
    )


class PlatformCapped(Exception):
    """平台内置模型的当日额度已用完。"""


def _record_platform_call(user_id: int, config: LLMConfig) -> None:
    """平台内置模型每次调用都计入日额度；用户自带 Key 不受平台额度限制。

    试聊、主动消息、告别信、同步蒸馏等入口原先都绕过了额度校验，用户可以无限
    白用平台模型。统一走这里计数，避免额度形同虚设。
    """
    if not getattr(config, "platform", False):
        return
    limit = load_platform_config().daily_limit
    if limit and store.count_platform_calls_since(user_id, _day_start_iso()) >= limit:
        raise PlatformCapped("今日平台模型额度已用完")
    store.add_platform_call(user_id)


def get_agent(user_id: int, persona: dict) -> PersonaAgent:
    key = f"{user_id}:{persona['id']}"
    stamp = f"{persona.get('updated_at') or ''}:{persona.get('status') or ''}"
    cached = _agents.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    agent = PersonaAgent(persona["dir"], config=build_config(user_id, persona))
    _agents[key] = (stamp, agent)
    return agent


def _require_persona(user_id: int, persona_id: int | None = None) -> dict:
    persona = (
        store.get_persona(user_id, persona_id) if persona_id else store.get_active_persona(user_id)
    )
    if persona is None:
        raise HTTPException(status_code=404, detail="尚未创建人格")
    return persona


def _require_admin(user: dict) -> None:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")


def _ensure_persona_quota(user_id: int) -> None:
    if store.count_personas_for_user(user_id) >= MAX_PERSONAS_PER_USER:
        raise HTTPException(
            status_code=400, detail=f"最多创建 {MAX_PERSONAS_PER_USER} 个 Agent"
        )


def _admin_usernames() -> set[str]:
    raw = os.getenv("PERSONA_ADMIN_USERS") or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _apply_admin_whitelist() -> None:
    """把白名单里的用户名提升为管理员，方便部署方开通后台。"""
    names = _admin_usernames()
    if not names:
        return
    for row in store.list_users():
        if (row.get("username") or "").lower() in names and (row.get("role") or "user") != "admin":
            store.set_user_role(row["id"], "admin")


# 运营告警的冷却时间与记录，避免同一故障在每个请求里反复刷屏。
ADMIN_ALERT_COOLDOWN_SECONDS = 900
_admin_alert_at: dict[str, float] = {}
_admin_alert_lock = threading.Lock()


def _notify_admins(kind: str, message: str, *, cooldown: int = ADMIN_ALERT_COOLDOWN_SECONDS) -> None:
    """给所有管理员写站内告警；同类告警在冷却期内只记一次。"""
    now = time.time()
    with _admin_alert_lock:
        if now - _admin_alert_at.get(kind, 0.0) < cooldown:
            return
        _admin_alert_at[kind] = now
    for row in store.list_users():
        if (row.get("role") or "user") == "admin":
            try:
                store.add_alert(row["id"], kind, message)
            except Exception:  # noqa: BLE001 - 告警失败不影响主流程
                log.exception("admin alert failed", extra={"event": "alert.admin_error"})


def _audit(actor: dict, action: str, target: str = "", detail: str = "") -> None:
    """记录管理员敏感操作；审计写入失败不影响主流程。"""
    try:
        store.add_audit(
            actor.get("id") or 0,
            actor.get("username") or "",
            action,
            target,
            detail,
        )
    except Exception:  # noqa: BLE001 - 审计失败不能阻断业务
        log.exception("audit write failed", extra={"event": "audit.error"})


def _channel_info(user_id: int, persona: dict) -> dict:
    binding = store.get_wechat_binding(user_id)
    token = binding["bridge_token"] if binding else ""
    endpoint = f"/v1/chat/completions/{token}" if token else ""
    phase = binding.get("phase") if binding else "idle"
    return {
        "wechat": {
            "bound": phase in {"logged-in", "running"},
            "phase": phase,
            "running": manager.is_running(user_id),
            "available": bool(wechat.WECLAW_BIN),
            "endpoint": endpoint,
            "token": token,
            "contact": persona.get("last_contact") or "",
        },
        "qq": {"enabled": False, "bound": False, "phase": "idle"},
    }


def _persona_summary(user_id: int, persona: dict, channel: dict | None = None) -> dict:
    channel = channel or _channel_info(user_id, persona)
    return {
        "id": persona["id"],
        "name": persona["name"],
        "target_name": persona.get("target_name"),
        "tag": persona.get("tag") or "",
        "avatar": persona.get("avatar") or "",
        "last_contact": persona.get("last_contact") or "",
        "status": persona.get("status") or "empty",
        "is_active": bool(persona.get("is_active")),
        "created_at": persona.get("created_at"),
        "updated_at": persona.get("updated_at"),
        "wechat_bound": channel["wechat"]["bound"],
        "wechat_running": channel["wechat"]["running"],
        "qq_bound": channel["qq"]["bound"],
    }


def _persona_detail(user_id: int, persona: dict) -> dict:
    directory = Path(persona["dir"]) if persona.get("dir") else None
    card = persona_files.read_card(directory, persona["name"]) if directory else {}
    style = {}
    if directory and (directory / "style.json").exists():
        try:
            style = json.loads((directory / "style.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            style = {}
    settings = persona_settings.load(persona.get("settings"))
    channel = _channel_info(user_id, persona)
    stickers = store.list_stickers(user_id, persona["id"])
    return {
        "persona": _persona_summary(user_id, persona, channel),
        "card": card,
        "style": style,
        "settings": settings,
        "channel": channel,
        "stickers": stickers,
        "turn_count": store.count_turns(user_id, persona["id"]),
        "farewell": store.get_farewell(user_id, persona["id"]),
    }


# --------------------------------------------------------------------------- #
# 微信绑定
# --------------------------------------------------------------------------- #

def _ensure_binding(user_id: int) -> dict:
    binding = store.get_wechat_binding(user_id)
    if binding is None:
        binding = store.upsert_wechat_binding(
            user_id,
            bridge_token=crypto.new_bridge_token(user_id),
            home_dir=str(workspace.home_dir(user_id)),
            phase="idle",
        )
    return binding


def _bridge(user_id: int):
    binding = _ensure_binding(user_id)
    if not wechat.WECLAW_BIN:
        raise HTTPException(status_code=400, detail="未配置 weclaw")
    return manager.get(user_id, workspace.home_dir(user_id), binding["bridge_token"])


def _agent_factory(user_id: int, persona: dict) -> PersonaAgent:
    return get_agent(user_id, persona)


def _outbox_send(user_id: int, recipient: str, text: str, media: str) -> bool:
    """出站队列的实际投递函数：发送失败返回 False 由队列退避重试。"""
    if not recipient or (not text and not media):
        return False
    binding = store.get_wechat_binding(user_id)
    if binding is None or not wechat.WECLAW_BIN:
        return False
    try:
        bridge = manager.get(user_id, workspace.home_dir(user_id), binding["bridge_token"])
        result = bridge.send(recipient, text=text, media=media)
    except Exception as error:  # noqa: BLE001 - 交给队列重试
        log.warning(
            "outbox send error",
            extra={"event": "outbox.send_error", "user_id": user_id, "error": str(error)},
        )
        return False
    return int(result.get("code", 1)) == 0


def _on_outbox_fail(row: dict, error: str) -> None:
    """出站消息重试耗尽时提醒用户检查微信是否掉线。"""
    user_id = int(row.get("user_id") or 0)
    if not user_id:
        return
    persona = store.get_active_persona(user_id)
    settings = persona_settings.load(persona.get("settings")) if persona else {}
    if not _safety(settings).get("offline_alert", True):
        return
    _notify(user_id, "send_failed", "有消息多次发送失败，微信可能已掉线，请重新扫码登录。")
    _notify_admins("send_failed", f"用户 #{user_id} 的出站消息多次发送失败，微信可能已掉线。")


_outbox = outbox.OutboxWorker(_outbox_send, on_fail=_on_outbox_fail)


def _proactive_send(
    user_id: int, to: str, text: str, persona: dict | None = None
) -> bool:
    """主动消息统一入队，由后台队列负责限速与重试。"""
    text = _clean_outbound(text)
    if not to or not text:
        return False
    if store.get_wechat_binding(user_id) is None:
        return False
    target = persona or store.get_active_persona(user_id)
    settings = persona_settings.load(target.get("settings")) if target else {}
    if _safety(settings).get("enabled", True) and safety.in_quiet_hours(settings, clock.now_local()):
        observability.METRICS.inc("proactive.quiet")
        return False
    if not _quota_ok(user_id, settings):
        observability.METRICS.inc("proactive.capped")
        _notify(user_id, "quota", "主动消息已达发送上限，已自动暂停，明天恢复。")
        return False
    text = _apply_filter(text, settings)
    if not text:
        observability.METRICS.inc("proactive.blocked")
        return False
    _outbox.enqueue(user_id, to, text=text)
    _record_send(user_id)
    return True


def _queue_media_replies(
    user_id: int,
    persona: dict | None,
    contact: str,
    stickers: list[dict],
    directives: list[dict],
) -> int:
    """把回复里的媒体指令落到出站队列，最多两条，避免刷屏。"""
    if not contact or not directives or not stickers:
        return 0
    target = persona or store.get_active_persona(user_id)
    settings = persona_settings.load(target.get("settings")) if target else None
    queued = 0
    for directive in directives[:2]:
        if settings is not None and not _quota_ok(user_id, settings):
            observability.METRICS.inc("reply.media_capped")
            _notify(user_id, "quota", "发送频率达到上限，部分表情包已暂缓。")
            break
        sticker = media_reply.choose_media(stickers, directive)
        path = (sticker or {}).get("path") or ""
        if not path:
            continue
        if _outbox.enqueue(user_id, contact, media=path):
            queued += 1
            _record_send(user_id)
            observability.METRICS.inc(f"reply.media.{directive.get('kind', 'media').lower()}")
    return queued


def _queue_text_segments(
    user_id: int, persona: dict | None, contact: str, segments: list[str], base_delay: float = 0.0
) -> int:
    """把分段回复的后续几条按打字节奏排进队列，模仿真人的多段发送。

    ``base_delay`` 是首条回复的拟人等待时间：后续分段必须在此基础上再排队，
    否则出站工作线程可能在首条还没送达前就把第二条发出去，出现顺序错乱。
    """
    if not contact or not segments:
        return 0
    target = persona or store.get_active_persona(user_id)
    settings = persona_settings.load(target.get("settings")) if target else {}
    delay = max(float(base_delay or 0.0), 0.0)
    queued = 0
    for segment in segments:
        if not _quota_ok(user_id, settings):
            observability.METRICS.inc("reply.split_capped")
            _notify(user_id, "quota", "发送频率达到上限，部分回复已暂缓。")
            break
        delay += humanize.segment_delay(segment)
        if _outbox.enqueue(user_id, contact, text=segment, delay_seconds=delay):
            queued += 1
            _record_send(user_id)
            observability.METRICS.inc("reply.split")
    return queued


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #

class Credentials(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


class PasswordOnly(BaseModel):
    password: str


class TotpCode(BaseModel):
    code: str


class RecoveryRequest(BaseModel):
    username: str
    contact: str = ""
    note: str = ""


class RoleChange(BaseModel):
    role: str


class AdminPasswordReset(BaseModel):
    password: str


class PersonaRequest(BaseModel):
    name: str
    target: str | None = None
    tag: str | None = None
    gender: str | None = None
    personality: str | None = None
    catchphrases: str | None = None
    style: str | None = None


class PersonaUpdate(BaseModel):
    name: str | None = None
    tag: str | None = None
    avatar: str | None = None
    card: dict | None = None


class PresetCreate(BaseModel):
    preset_id: str
    name: str | None = None


class SettingsPatch(BaseModel):
    settings: dict


class StickerPatch(BaseModel):
    name: str | None = None


class ModelConfigRequest(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class PlatformConfigRequest(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    enabled: bool | None = None
    per_turn_cost: int | None = None
    new_user_gift: int | None = None
    default_credit_days: int | None = None
    distill_ticket_price: int | None = None
    distill_ticket_gift: int | None = None


class CreditGrantRequest(BaseModel):
    delta: int
    reason: str | None = None
    credit_days: int | None = None


class CoinGrantRequest(BaseModel):
    delta: int
    reason: str | None = None


class RedeemRequest(BaseModel):
    code: str


class StatusChange(BaseModel):
    status: str


class RedemptionCodeRequest(BaseModel):
    count: int = 10
    coins: int
    batch: str | None = None
    note: str | None = None


class PackageRequest(BaseModel):
    id: int | None = None
    name: str
    credits: int
    coins: int = 0
    price_cents: int = 0
    badge: str | None = None
    sort: int = 0
    active: bool = True
    validity_days: int = 30
    bonus_credits: int = 0
    bonus_tickets: int = 0


class ProfileRequest(BaseModel):
    nickname: str | None = None
    avatar: str | None = None


class ReportRequest(BaseModel):
    category: str | None = None
    detail: str | None = None
    persona_id: int | None = None


class PasteRequest(BaseModel):
    text: str = Field(max_length=2_000_000)


class IngestRequest(BaseModel):
    target: str | None = None
    persona_id: int | None = None


class DistillRequest(BaseModel):
    use_llm: bool = True
    persona_id: int | None = None


class OAIMessage(BaseModel):
    role: str
    content: object = None


class OAIRequest(BaseModel):
    model: str | None = None
    messages: list[OAIMessage] = []
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

@app.get("/")
def landing(request: Request):
    if accounts.session_user(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/app", status_code=302)
    return FileResponse(WEB_DIR / "landing.html")


@app.get("/login")
def login_page(request: Request):
    if accounts.session_user(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/app", status_code=302)
    return FileResponse(WEB_DIR / "login.html")


@app.get("/app")
def app_page(request: Request):
    return _page("app.html")(request)


@app.get("/privacy")
def privacy_page():
    return FileResponse(WEB_DIR / "privacy.html")


@app.get("/terms")
def terms_page():
    return FileResponse(WEB_DIR / "terms.html")


@app.get("/offline.html")
def offline_page():
    return FileResponse(WEB_DIR / "offline.html")


@app.get("/sw.js")
def service_worker():
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")


@app.get("/admin")
def admin_page(request: Request):
    user = accounts.session_user(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return RedirectResponse("/admin/login", status_code=302)
    if user.get("role") != "admin":
        return RedirectResponse("/app", status_code=302)
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/admin/login")
def admin_login_page(request: Request):
    user = accounts.session_user(request.cookies.get(SESSION_COOKIE))
    if user is not None and user.get("role") == "admin":
        return RedirectResponse("/admin", status_code=302)
    return FileResponse(WEB_DIR / "admin_login.html")


@app.get("/app/admin")
def admin_page_legacy():
    return RedirectResponse("/admin", status_code=302)


@app.get("/app/train")
def app_train(request: Request):
    return RedirectResponse("/app", status_code=302)


@app.get("/app/settings")
def app_settings(request: Request):
    return _page("settings.html")(request)


@app.get("/app/wechat")
def app_wechat(request: Request):
    return RedirectResponse("/app", status_code=302)


@app.get("/app/agent/{persona_id}")
def app_agent(persona_id: int, request: Request):
    return _page("agent.html")(request)


@app.get("/app/create/distill")
def app_create_distill(request: Request):
    return _page("create_distill.html")(request)


@app.get("/app/memory")
def app_memory(request: Request):
    return _page("memory.html")(request)


@app.get("/app/timeline")
def app_timeline(request: Request):
    return _page("timeline.html")(request)


@app.get("/app/moments")
def app_moments(request: Request):
    return _page("moments.html")(request)


# --------------------------------------------------------------------------- #
# 账号 API
# --------------------------------------------------------------------------- #

@app.post("/api/auth/register")
def register(payload: Credentials, request: Request) -> JSONResponse:
    ip_key = f"ip:{_client_ip(request)}"
    if _auth_blocked(ip_key):
        raise HTTPException(
            status_code=429,
            detail="操作过于频繁，请稍后再试",
            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
        )
    if _register_blocked(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="注册过于频繁，请稍后再试",
            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
        )
    try:
        user = accounts.register(payload.username, payload.password)
    except accounts.AccountError as error:
        _auth_note(ip_key)
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    _auth_clear(ip_key)
    _register_note(_client_ip(request))
    workspace.ensure_user(user["id"])
    # 管理员白名单只在启动时对已存在的账号生效，注册接口不再自动提权，
    # 避免注册一个大小写变体的同名账号即可获得后台权限。
    platform = load_platform_config()
    if platform.new_user_gift > 0:
        store.grant_credits(
            user["id"], platform.new_user_gift, reason="新用户注册赠送", actor="system",
            ref="register", expires_days=platform.default_credit_days, source="gift",
        )
    if platform.distill_ticket_gift > 0:
        store.grant_distill_tickets(
            user["id"], platform.distill_ticket_gift, reason="新用户注册赠送",
            actor="system", ref="register", idem="register",
        )
    return _login_response(user, request)


@app.post("/api/auth/login")
def login(payload: Credentials, request: Request) -> JSONResponse:
    ip_key = f"ip:{_client_ip(request)}"
    user_key = f"user:{(payload.username or '').strip().lower()}"
    if _auth_blocked(ip_key, user_key):
        raise HTTPException(
            status_code=429,
            detail="尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
        )
    try:
        user = accounts.authenticate(payload.username, payload.password)
    except accounts.AccountError as error:
        _auth_note(ip_key, user_key)
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    _auth_clear(ip_key, user_key)
    if user.get("totp_enabled"):
        return _login_response(user, request, pending_totp=True)
    return _login_response(user, request)


@app.post("/api/auth/2fa")
def login_2fa(payload: TotpCode, request: Request) -> JSONResponse:
    """校验动态验证码，完成处于待验证状态的登录。"""
    token = request.cookies.get(SESSION_COOKIE)
    user = accounts.session_user(token, allow_pending=True)
    if user is None or not user.get("totp_pending"):
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    ip_key = f"ip:{_client_ip(request)}"
    user_key = f"2fa:{(user.get('username') or '').lower()}"
    if _auth_blocked(ip_key, user_key):
        raise HTTPException(
            status_code=429,
            detail="验证码尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
        )
    if not accounts.verify_totp(user.get("totp_secret") or "", payload.code):
        _auth_note(ip_key, user_key)
        raise HTTPException(status_code=401, detail="验证码不正确")
    _auth_clear(ip_key, user_key)
    store.mark_session_verified(token)
    return JSONResponse({"ok": True, "user": _public_user(user)})


@app.get("/api/account/security")
def account_security(user: dict = Depends(current_user)) -> dict:
    return {
        "totp_enabled": bool(user.get("totp_enabled")),
        "totp_pending": bool(user.get("totp_secret")) and not bool(user.get("totp_enabled")),
    }


@app.post("/api/account/2fa/setup")
def setup_2fa(user: dict = Depends(current_user)) -> dict:
    """生成验证器密钥（尚未启用，需用验证码确认后才生效）。"""
    secret = accounts.generate_totp_secret()
    store.set_user_totp(user["id"], secret, False)
    return {"secret": secret, "uri": accounts.totp_uri(secret, user.get("username") or "")}


@app.post("/api/account/2fa/enable")
def enable_2fa(payload: TotpCode, user: dict = Depends(current_user)) -> dict:
    secret = user.get("totp_secret") or ""
    if not secret:
        raise HTTPException(status_code=400, detail="请先生成验证器密钥")
    if not accounts.verify_totp(secret, payload.code):
        raise HTTPException(status_code=400, detail="验证码不正确")
    store.set_user_totp(user["id"], secret, True)
    return {"ok": True, "totp_enabled": True}


@app.post("/api/account/2fa/disable")
def disable_2fa(payload: PasswordOnly, user: dict = Depends(current_user)) -> dict:
    if user.get("password_hash") and not accounts.verify_password(
        payload.password, user.get("password_salt"), user.get("password_hash")
    ):
        raise HTTPException(status_code=401, detail="密码不正确")
    store.set_user_totp(user["id"], "", False)
    return {"ok": True, "totp_enabled": False}


@app.post("/api/auth/recover")
def recover_password(payload: RecoveryRequest, request: Request) -> dict:
    """提交找回密码申请：统一返回成功，避免暴露用户名是否存在。"""
    ip_key = f"recover:{_client_ip(request)}"
    if _auth_blocked(ip_key):
        raise HTTPException(
            status_code=429,
            detail="提交过于频繁，请稍后再试",
            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
        )
    _auth_note(ip_key)
    username = (payload.username or "").strip()
    user = store.get_user_by_username(username) if username else None
    store.add_recovery_request(
        username, payload.contact or "", payload.note or "", user["id"] if user else None
    )
    if user is not None:
        _notify_admins("recovery", f"用户 {username} 申请找回密码，请在后台重置密码后联系 ta。")
    return {"ok": True, "message": "申请已提交，管理员会尽快联系你"}


@app.post("/api/auth/logout")
def logout(request: Request) -> dict:
    accounts.end_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.post("/api/account/logout-all")
def logout_all(user: dict = Depends(current_user)) -> JSONResponse:
    """注销该账号在所有设备上的登录状态，包括当前浏览器。"""
    store.delete_user_sessions(user["id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    personas = store.list_personas(user["id"])
    active = store.get_active_persona(user["id"])
    config_row = store.get_model_config(user["id"]) or {}
    binding = store.get_wechat_binding(user["id"])
    channel = _channel_info(user["id"], active or {})
    platform_ready = load_platform_config().ready
    has_key = own_key_usable(config_row)
    wechat_bound = bool(binding and binding.get("phase") in {"logged-in", "running"})
    credits = store.credits_summary(user["id"])
    platform_config = load_platform_config()
    credits["per_turn_cost"] = platform_config.per_turn_cost
    credits["coins"] = store.get_coins(user["id"])
    credits["distill_tickets"] = store.get_distill_tickets(user["id"])
    credits["distill_ticket_price"] = platform_config.distill_ticket_price
    return {
        "user": _public_user(user),
        "personas": [_persona_summary(user["id"], persona, channel) for persona in personas],
        "active_persona_id": active["id"] if active else None,
        "config": {
            "base_url": config_row.get("base_url") or "https://api.deepseek.com/v1",
            "model": config_row.get("model") or "deepseek-chat",
            "has_key": has_key,
            "platform_available": platform_ready,
        },
        "wechat": {
            "bound": wechat_bound,
            "phase": binding.get("phase") if binding else "idle",
            "running": manager.is_running(user["id"]),
        },
        "onboarding": {
            "has_persona": bool(personas),
            "has_key": has_key or platform_ready,
            "wechat_bound": wechat_bound,
            "done": bool(personas) and (has_key or platform_ready) and wechat_bound,
        },
        "wechat_login_enabled": wechat_login.enabled(),
        "presets": presets.list_presets(),
        "credits": credits,
        "notifications": {"unread": store.count_unread_alerts(user["id"])},
    }


@app.post("/api/profile")
def update_profile(payload: ProfileRequest, user: dict = Depends(current_user)) -> dict:
    nickname = None
    avatar = None
    if payload.nickname is not None:
        nickname = payload.nickname.strip()
        if len(nickname) > 20:
            raise HTTPException(status_code=400, detail="昵称最多 20 个字")
    if payload.avatar is not None:
        avatar = payload.avatar.strip()
        if avatar:
            if not avatar.startswith("data:image/"):
                raise HTTPException(status_code=400, detail="头像格式不正确")
            if len(avatar) > 400_000:
                raise HTTPException(status_code=400, detail="头像图片过大，请换一张")
    store.set_user_profile(user["id"], nickname=nickname, avatar=avatar)
    return {"ok": True, "user": _public_user(store.get_user(user["id"]) or user)}


@app.post("/api/account/password")
def change_password(
    payload: PasswordChange, request: Request, user: dict = Depends(current_user)
) -> JSONResponse:
    try:
        accounts.change_password(user["id"], payload.current, payload.new)
    except accounts.AccountError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    # 改密后让其他设备的会话立即失效，并给当前浏览器换发新会话。
    store.delete_user_sessions(user["id"])
    return _login_response(user, request)


@app.post("/api/account/delete")
def delete_account(payload: PasswordChange, user: dict = Depends(current_user)) -> dict:
    if user.get("password_hash") and not accounts.verify_password(
        payload.current, user.get("password_salt"), user.get("password_hash")
    ):
        raise HTTPException(status_code=401, detail="密码不正确")
    manager.stop(user["id"])
    root = workspace.user_root(user["id"])
    if root.exists():
        trash = workspace.data_root() / "deleted"
        trash.mkdir(parents=True, exist_ok=True)
        root.rename(trash / f"{user['id']}-{int(time.time())}")
    store.purge_user_content(user["id"])
    store.delete_user(user["id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------- #
# 微信扫码登录（预留）
# --------------------------------------------------------------------------- #

@app.get("/api/auth/wechat/url")
def wechat_authorize() -> dict:
    if not wechat_login.enabled():
        raise HTTPException(status_code=404, detail="未启用微信扫码登录")
    state = uuid.uuid4().hex
    store.create_login_state(state)
    return {"url": wechat_login.authorize_url(state)}


@app.get("/api/auth/wechat/callback")
def wechat_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if not wechat_login.enabled():
        raise HTTPException(status_code=404, detail="未启用微信扫码登录")
    if not store.consume_login_state(state):
        return RedirectResponse("/login?error=state", status_code=302)
    try:
        identity = wechat_login.exchange_code(code)
    except RuntimeError:
        return RedirectResponse("/login?error=wechat", status_code=302)
    user = store.get_user_by_openid(identity["openid"])
    if user is None:
        user = store.create_user(
            username=None,
            nickname=identity.get("nickname"),
            avatar=identity.get("avatar"),
            wechat_openid=identity["openid"],
            wechat_unionid=identity.get("unionid"),
        )
        workspace.ensure_user(user["id"])
    response = RedirectResponse("/app", status_code=302)
    token = accounts.start_session(user["id"])
    response.set_cookie(
        SESSION_COOKIE, token, max_age=7 * 24 * 3600, httponly=True,
        samesite="lax", secure=_secure_cookie(request), path="/",
    )
    return response


# --------------------------------------------------------------------------- #
# 人格 API
# --------------------------------------------------------------------------- #

@app.get("/api/personas")
def list_personas(user: dict = Depends(current_user)) -> dict:
    channel = _channel_info(user["id"], store.get_active_persona(user["id"]) or {})
    return {
        "personas": [
            _persona_summary(user["id"], persona, channel)
            for persona in store.list_personas(user["id"])
        ]
    }


@app.get("/api/presets")
def api_presets(user: dict = Depends(current_user)) -> dict:
    return {"presets": presets.list_presets()}


def _new_persona(user_id: int, name: str, target: str, tag: str) -> tuple[dict, Path]:
    workspace.ensure_user(user_id)
    placeholder = store.create_persona(user_id, name, directory="", target_name=target, tag=tag)
    directory = workspace.ensure_persona(user_id, placeholder["id"])
    store.update_persona(user_id, placeholder["id"], dir=str(directory))
    return store.get_persona(user_id, placeholder["id"]), directory  # type: ignore[return-value]


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def _save_card(persona: dict, name: str, card: dict) -> None:
    directory = Path(persona["dir"])
    directory.mkdir(parents=True, exist_ok=True)
    style = _read_json(directory / "style.json", persona_files.empty_style())
    old_profile = _read_json(directory / "profile.json", {})
    meta = {
        "used_llm": old_profile.get("used_llm", False),
        "pair_count": old_profile.get("pair_count", 0),
        "senders": old_profile.get("senders", {}),
        "sources": old_profile.get("sources", []),
        "preset": old_profile.get("preset", ""),
    }
    persona_files.write_files(directory, name, card, style, meta)


def _distill_error_info(error: Exception) -> tuple[str, str]:
    """把蒸馏异常归类成可操作的错误类型与提示，便于前端区分处理。"""
    text = str(error or "").strip()
    low = text.lower()
    if isinstance(error, PlatformCapped) or "额度" in text:
        return "platform_quota", text or "平台今日额度已用尽，请稍后重试。"
    if any(key in low for key in ("invalid api key", "unauthorized", "401", "authentication")):
        return "llm_auth", "模型 API Key 无效或已过期，请到“我的 - 模型设置”更新后重试。"
    if any(key in low for key in ("rate limit", "429", "too many requests")):
        return "llm_rate", "模型接口当前限流，请等几分钟后重试。"
    if any(key in low for key in ("timed out", "timeout", "connection", "network", "unreachable", "ssl")):
        return "llm_network", "连接模型服务失败，请检查网络后重试。"
    if any(key in low for key in ("json", "decode", "解析", "encoding")):
        return "source_invalid", "聊天记录解析失败，请确认上传的是微信导出的原始聊天文件后重试。"
    if any(key in low for key in ("no such file", "not found", "不存在", "没有可用", "empty")):
        return "source_missing", "没有找到可用的聊天记录，请重新上传后再试。"
    return "unknown", text or "蒸馏失败，请稍后重试。"


def _run_distill_task(task_id: str, user_id: int, persona_id: int, use_llm: bool = True) -> None:
    try:
        persona = store.get_persona(user_id, persona_id)
        if persona is None:
            _update_task(task_id, status="error", step="失败", error="人格不存在", error_kind="missing")
            _refund_distill_ticket(user_id, task_id)
            observability.METRICS.inc("distill.error")
            return
        directory = Path(persona["dir"])
        hints = _read_json(directory / "persona_hints.json", None)
        _update_task(task_id, status="running", progress=8, step="准备素材")
        has_messages = (directory / "messages.jsonl").exists()
        if not has_messages:
            source = _last_batch.get(user_id)
            if source is None or not Path(source).exists():
                raw = workspace.raw_dir(user_id)
                source = raw if raw.exists() and any(raw.iterdir()) else None
            if source is None:
                store.update_persona(user_id, persona_id, status="ready")
                _update_task(task_id, status="done", progress=100, step="完成",
                             result={"profile": {"name": persona["name"], "message_count": 0,
                                                 "used_llm": False}})
                _refund_distill_ticket(user_id, task_id, "未检测到素材退还")
                observability.METRICS.inc("distill.empty")
                return
            _update_task(task_id, progress=28, step="解析聊天记录")
            target = persona.get("target_name") or persona["name"]
            ingest.run(Path(source), directory, target)
        _update_task(task_id, progress=58, step="AI 分析身份、灵魂与记忆")
        config_obj = build_config(user_id, persona)
        if use_llm:
            # 平台内置模型要计入当日额度；自带 Key 不受限。
            try:
                _record_platform_call(user_id, config_obj)
            except PlatformCapped as error:
                observability.METRICS.inc("distill.platform_capped")
                raise RuntimeError(f"{error}，可稍后重试或改用自带 API Key") from error
        result = distill.run(directory, config=config_obj, use_llm=use_llm, hints=hints)
        _agents.pop(f"{user_id}:{persona_id}", None)
        store.update_persona(user_id, persona_id, status="ready")
        _update_task(task_id, status="done", progress=100, step="完成",
                     result={"profile": result["profile"]})
        observability.METRICS.inc("distill.done")
    except Exception as error:  # noqa: BLE001
        kind, message = _distill_error_info(error)
        _update_task(task_id, status="error", step="失败", error=message, error_kind=kind)
        _refund_distill_ticket(user_id, task_id)
        observability.METRICS.inc("distill.error")
        log.warning(
            "distill failed",
            extra={"event": "distill.error", "user_id": user_id, "persona_id": persona_id,
                   "error_kind": kind, "error": str(error)},
        )


@app.post("/api/personas")
def create_persona(payload: PersonaRequest, user: dict = Depends(current_user)) -> dict:
    _ensure_persona_quota(user["id"])
    name = (payload.name or "").strip() or "未命名人格"
    persona, directory = _new_persona(user["id"], name, payload.target or name, payload.tag or "")
    card = persona_files.blank_card(name)
    hints = {
        "name": name,
        "gender": (payload.gender or "").strip(),
        "personality": (payload.personality or "").strip(),
        "catchphrases": (payload.catchphrases or "").strip(),
        "style": (payload.style or "").strip(),
    }
    card["identity"]["gender"] = hints["gender"]
    card["personality"] = distill._split_list(hints["personality"])
    card["soul"]["speaking_style"] = distill._split_list(hints["style"])
    card["soul"]["catchphrases"] = distill._split_list(hints["catchphrases"])
    if any(hints[key] for key in ("gender", "personality", "catchphrases", "style")):
        (directory / "persona_hints.json").write_text(
            json.dumps(hints, ensure_ascii=False), encoding="utf-8"
        )
    persona_files.write_files(directory, name, card, persona_files.empty_style())
    return _persona_detail(user["id"], store.get_persona(user["id"], persona["id"]))  # type: ignore[arg-type]


@app.post("/api/personas/distill")
async def create_and_distill(
    name: str = Form(""),
    tag: str = Form(""),
    gender: str = Form(""),
    personality: str = Form(""),
    catchphrases: str = Form(""),
    style: str = Form(""),
    target: str = Form(""),
    client_id: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    user: dict = Depends(current_user),
) -> dict:
    clean_id = (client_id or "").strip()[:64]
    # 幂等：同一次提交（同一 client_id）重试/刷新时复用已建人格，避免重复建号。
    if clean_id:
        existing = store.find_task_by_client(user["id"], "distill", clean_id)
        persona_id = existing.get("persona_id") if existing else None
        if persona_id and store.get_persona(user["id"], int(persona_id)):
            if existing.get("status") == "error":
                task_id = uuid.uuid4().hex
                _reserve_distill_ticket(user["id"], task_id)
                _new_task(user["id"], int(persona_id), "distill", client_id=clean_id,
                          task_id=task_id)
                threading.Thread(
                    target=_run_distill_task,
                    args=(task_id, user["id"], int(persona_id)),
                    daemon=True,
                ).start()
            else:
                task_id = existing["id"]
            persona = store.get_persona(user["id"], int(persona_id))
            return {
                "persona": _persona_summary(user["id"], persona),  # type: ignore[arg-type]
                "task_id": task_id,
                "saved": 0,
                "reused": True,
            }
    _ensure_persona_quota(user["id"])
    task_id = uuid.uuid4().hex
    # 先预扣蒸馏券，后续任一步骤失败都退还，避免占用额度。
    _reserve_distill_ticket(user["id"], task_id)
    try:
        clean = (name or "").strip() or "未命名人格"
        persona, directory = _new_persona(user["id"], clean, (target or "").strip() or clean, tag)
        hints = {
            "name": clean,
            "gender": gender.strip(),
            "personality": personality.strip(),
            "catchphrases": catchphrases.strip(),
            "style": style.strip(),
        }
        (directory / "persona_hints.json").write_text(
            json.dumps(hints, ensure_ascii=False), encoding="utf-8"
        )
        card = persona_files.blank_card(clean)
        card["identity"]["gender"] = hints["gender"]
        card["personality"] = distill._split_list(hints["personality"])
        card["soul"]["speaking_style"] = distill._split_list(hints["style"])
        card["soul"]["catchphrases"] = distill._split_list(hints["catchphrases"])
        persona_files.write_files(directory, clean, card, persona_files.empty_style())

        saved = 0
        if files:
            if len(files) > MAX_FILES:
                raise HTTPException(status_code=413, detail=f"单次最多上传 {MAX_FILES} 个文件")
            batch = workspace.new_batch(user["id"], "distill")
            for item in files:
                data = await item.read()
                if len(data) > MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail=f"文件过大：{item.filename}")
                destination = workspace.safe_join(batch, item.filename or "chat.txt")
                destination.write_bytes(data)
                saved += 1
            if saved:
                _last_batch[user["id"]] = batch
        _new_task(user["id"], persona["id"], "distill", client_id=clean_id, task_id=task_id)
    except Exception:
        _refund_distill_ticket(user["id"], task_id)
        raise
    threading.Thread(
        target=_run_distill_task, args=(task_id, user["id"], persona["id"]), daemon=True
    ).start()
    return {
        "persona": _persona_summary(user["id"], store.get_persona(user["id"], persona["id"])),
        "task_id": task_id,
        "saved": saved,
        "reused": False,
    }


@app.post("/api/personas/{persona_id}/redistill")
def redistill(persona_id: int, payload: DistillRequest, user: dict = Depends(current_user)) -> dict:
    _require_persona(user["id"], persona_id)
    task_id = uuid.uuid4().hex
    _reserve_distill_ticket(user["id"], task_id)
    _new_task(user["id"], persona_id, "redistill", task_id=task_id)
    threading.Thread(
        target=_run_distill_task, args=(task_id, user["id"], persona_id, payload.use_llm), daemon=True
    ).start()
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, user: dict = Depends(current_user)) -> dict:
    task = store.get_task(task_id)
    if task is None or task.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_payload(task)


@app.post("/api/personas/from-preset")
def create_from_preset(payload: PresetCreate, user: dict = Depends(current_user)) -> dict:
    _ensure_persona_quota(user["id"])
    preset = presets.get_preset(payload.preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="预置角色不存在")
    name = (payload.name or "").strip() or preset["name"]
    tag = "、".join(preset["tags"][:2])
    persona, directory = _new_persona(user["id"], name, name, tag)
    persona_files.write_files(
        directory, name, presets.preset_card(preset), persona_files.empty_style(),
        {"preset": preset["id"]},
    )
    store.update_persona(user["id"], persona["id"], status="ready")
    return _persona_detail(user["id"], store.get_persona(user["id"], persona["id"]))  # type: ignore[arg-type]


@app.get("/api/personas/{persona_id}")
def persona_detail(persona_id: int, user: dict = Depends(current_user)) -> dict:
    return _persona_detail(user["id"], _require_persona(user["id"], persona_id))


@app.patch("/api/personas/{persona_id}")
def update_persona(persona_id: int, payload: PersonaUpdate, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], persona_id)
    name = (payload.name or "").strip() or persona["name"]
    directory = Path(persona["dir"])
    existing = persona_files.read_card(directory, name)
    patch = payload.card or {}
    for key in ("identity", "user", "soul"):
        if isinstance(patch.get(key), dict):
            existing.setdefault(key, {})
            existing[key].update(patch[key])
    if isinstance(patch.get("memories"), list):
        existing["memories"] = [item for item in patch["memories"] if isinstance(item, dict)]
    for key, value in patch.items():
        if key not in ("identity", "user", "soul", "memories"):
            existing[key] = value
    _save_card(persona, name, existing)

    fields: dict = {}
    if name != persona["name"]:
        fields["name"] = name
    if payload.tag is not None:
        fields["tag"] = payload.tag.strip()
    if payload.avatar is not None:
        fields["avatar"] = payload.avatar.strip()
    if fields:
        store.update_persona(user["id"], persona_id, **fields)
    _agents.pop(f"{user['id']}:{persona_id}", None)
    return _persona_detail(user["id"], store.get_persona(user["id"], persona_id))  # type: ignore[arg-type]


@app.delete("/api/personas/{persona_id}")
def delete_persona(persona_id: int, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], persona_id)
    store.delete_persona(user["id"], persona_id)
    _agents.pop(f"{user['id']}:{persona_id}", None)
    for key in [key for key in _reply_cache if key[0] == persona_id]:
        _reply_cache.pop(key, None)
    _remove_persona_dir(user["id"], persona.get("dir"))
    return {"ok": True}


def _remove_persona_dir(user_id: int, raw_dir: str | None) -> None:
    """删除人格后清理磁盘目录，仅允许删除用户工作区内的路径。"""
    if not raw_dir:
        return
    directory = Path(raw_dir)
    try:
        resolved = directory.resolve()
        root = workspace.personas_root(user_id).resolve()
    except OSError:
        return
    if resolved == root or root not in resolved.parents:
        return
    shutil.rmtree(resolved, ignore_errors=True)


@app.post("/api/personas/{persona_id}/reset")
def reset_session(persona_id: int, user: dict = Depends(current_user)) -> dict:
    _require_persona(user["id"], persona_id)
    store.clear_turns(user["id"], persona_id)
    for key in [key for key in _reply_cache if key[0] == persona_id]:
        _reply_cache.pop(key, None)
    return {"ok": True, "turn_count": 0}


@app.put("/api/personas/{persona_id}/settings")
def save_settings(persona_id: int, payload: SettingsPatch, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], persona_id)
    merged = persona_settings.merge_patch(persona.get("settings"), payload.settings)
    merged = persona_settings.validate(merged)
    store.update_persona(user["id"], persona_id, settings=persona_settings.dump(merged))
    return {"settings": merged}


@app.get("/api/personas/{persona_id}/stickers")
def list_stickers(persona_id: int, user: dict = Depends(current_user)) -> dict:
    _require_persona(user["id"], persona_id)
    return {"stickers": [dict(item) for item in store.list_stickers(user["id"], persona_id)]}


@app.post("/api/personas/{persona_id}/stickers")
async def upload_sticker(
    persona_id: int,
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    persona = _require_persona(user["id"], persona_id)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="文件过大")
    directory = Path(persona["dir"]) / "stickers"
    target, display = _save_upload_image(data, file.filename or "sticker.png", directory)
    sticker = store.add_sticker(user["id"], persona_id, display, str(target))
    return {"sticker": sticker, "stickers": store.list_stickers(user["id"], persona_id)}


@app.delete("/api/personas/{persona_id}/stickers/{sticker_id}")
def remove_sticker(persona_id: int, sticker_id: int, user: dict = Depends(current_user)) -> dict:
    _require_persona(user["id"], persona_id)
    if store.delete_sticker(user["id"], persona_id, sticker_id) is None:
        raise HTTPException(status_code=404, detail="表情不存在")
    return {"ok": True, "stickers": store.list_stickers(user["id"], persona_id)}


@app.get("/public/sticker/{sticker_id}")
def public_sticker(sticker_id: int, token: str = "") -> FileResponse:
    sticker = store.get_sticker_by_id(sticker_id)
    if sticker is None:
        raise HTTPException(status_code=404, detail="表情不存在")
    binding = store.get_wechat_binding(sticker["user_id"])
    if binding is None or not secrets.compare_digest(str(binding.get("bridge_token") or ""), token or ""):
        raise HTTPException(status_code=403, detail="无权访问")
    path = Path(sticker["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path)


@app.post("/api/wechat/media/{bridge_token}")
async def wechat_media(bridge_token: str, request: Request) -> dict:
    """微信桥接回调：把聊天框里收到的图片/表情保存为对应人格的表情包素材。"""
    if crypto.signed_bridge_token(bridge_token) and not crypto.verify_bridge_token(bridge_token):
        raise HTTPException(status_code=401, detail="桥接令牌校验失败")
    if _rate_limited(bridge_token):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    binding = store.get_binding_by_token(bridge_token)
    if binding is None:
        raise HTTPException(status_code=404, detail="无效的桥接令牌")
    user_id = binding["user_id"]
    persona = store.get_persona(user_id, binding["persona_id"]) if binding.get("persona_id") else None
    persona = persona or store.get_active_persona(user_id)
    if persona is None:
        raise HTTPException(status_code=400, detail="该用户尚未创建人格")

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="缺少图片文件")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="文件过大")

    raw_name = getattr(upload, "filename", None) or "wechat.png"
    directory = Path(persona["dir"]) / "stickers"
    target, display = _save_upload_image(data, raw_name, directory)
    sticker = store.add_sticker(user_id, persona["id"], display, str(target))

    contact = (request.headers.get("X-WeChat-From") or "").strip()
    if contact:
        store.update_persona(user_id, persona["id"], last_contact=contact)
        store.touch_contact(user_id, persona["id"], contact)
    return {"ok": True, "persona_id": persona["id"], "sticker": dict(sticker)}


@app.get("/api/personas/{persona_id}/channel")
def persona_channel(persona_id: int, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], persona_id)
    return _channel_info(user["id"], persona)


@app.put("/api/personas/{persona_id}/channel/contact")
def set_channel_contact(persona_id: int, payload: dict, user: dict = Depends(current_user)) -> dict:
    _require_persona(user["id"], persona_id)
    contact = str(payload.get("contact") or "").strip()
    store.update_persona(user["id"], persona_id, last_contact=contact)
    return {"contact": contact}


# --------------------------------------------------------------------------- #
# 长期记忆与联系人
# --------------------------------------------------------------------------- #

@app.get("/api/personas/{persona_id}/memories")
def list_memories_api(
    persona_id: int, contact: str = "", user: dict = Depends(current_user)
) -> dict:
    persona = _require_persona(user["id"], persona_id)
    settings = persona_settings.load(persona.get("settings"))
    contacts = store.list_contacts(user["id"], persona_id)
    selected = contact.strip()
    if not selected and contacts:
        selected = contacts[0]["contact_key"]
    items = store.list_memories(user["id"], persona_id, selected) if selected else []
    return {
        "enabled": bool(settings["model"].get("long_term_memory", True)),
        "extract_every": int(settings["model"].get("memory_extract_every") or 6),
        "contacts": contacts,
        "selected": selected,
        "memories": items,
        "total": store.count_memories(user["id"], persona_id),
    }


@app.delete("/api/personas/{persona_id}/memories/{memory_id}")
def delete_memory_api(
    persona_id: int, memory_id: int, user: dict = Depends(current_user)
) -> dict:
    _require_persona(user["id"], persona_id)
    if not store.delete_memory(user["id"], persona_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": True}


@app.put("/api/personas/{persona_id}/memories/{memory_id}")
def update_memory_api(
    persona_id: int, memory_id: int, payload: dict, user: dict = Depends(current_user)
) -> dict:
    _require_persona(user["id"], persona_id)
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    if len(content) > MAX_MEMORY_CHARS:
        raise HTTPException(status_code=400, detail=f"记忆内容不能超过 {MAX_MEMORY_CHARS} 字")
    kind = payload.get("kind")
    if not store.update_memory(
        user["id"], persona_id, memory_id, content, str(kind) if kind else None
    ):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"updated": True}


@app.post("/api/personas/{persona_id}/memories/clear")
def clear_memories_api(
    persona_id: int, payload: dict, user: dict = Depends(current_user)
) -> dict:
    _require_persona(user["id"], persona_id)
    contact = str(payload.get("contact") or "").strip()
    removed = store.clear_memories(user["id"], persona_id, contact or None)
    return {"removed": removed}


@app.get("/api/personas/{persona_id}/timeline")
def persona_timeline(
    persona_id: int, contact: str = "", user: dict = Depends(current_user)
) -> dict:
    """回忆时间线：逐日对话统计 + 长期记忆 + 主动消息，按人隔离。"""
    persona = _require_persona(user["id"], persona_id)
    data = store.timeline(user["id"], persona_id, contact.strip())
    contacts = store.list_contacts(user["id"], persona_id)
    return {
        "persona": {"id": persona["id"], "name": persona["name"]},
        "contacts": contacts,
        "farewell": store.get_farewell(user["id"], persona_id),
        **data,
    }


# --------------------------------------------------------------------------- #
# 朋友圈 API
# --------------------------------------------------------------------------- #

def _platform_per_turn_cost() -> int:
    config = load_platform_config()
    if config is None or not config.ready:
        return 0
    return int(getattr(config, "per_turn_cost", 0) or 0)


def _moment_cost_for(agent) -> int:
    """朋友圈按轮计费：仅当分身使用平台内置模型时收费，自带 Key 免费。"""
    if not getattr(getattr(agent, "config", None), "platform", False):
        return 0
    return _platform_per_turn_cost()


def _moment_per_turn_cost(user_id: int, persona: dict) -> int:
    """列表页展示用的单价：先判断该分身是否会走平台内置模型。"""
    try:
        uses_platform = bool(build_config(user_id, persona).platform)
    except Exception:  # noqa: BLE001
        uses_platform = False
    return _platform_per_turn_cost() if uses_platform else 0


def _moment_day_start_iso() -> str:
    now = clock.now_local()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat()


def _moment_payload(moment: dict) -> dict:
    payload = dict(moment)
    payload["has_sticker"] = bool(int(moment.get("sticker_id") or 0))
    return payload


def _moment_reply_context(moment: dict, comment: str) -> str:
    return (
        "【朋友圈】\n"
        f"你刚发的动态：{str(moment.get('content') or '')[:200]}\n"
        f"对方在动态下的评论：{comment[:200]}\n"
        "请像在评论区回复一样，以角色本人的身份简短回应对方，只输出回复内容本身。"
    )


def _require_moment(user: dict, moment_id: int) -> dict:
    moment = store.get_moment(user["id"], moment_id)
    if moment is None:
        raise HTTPException(status_code=404, detail="动态不存在")
    return moment


@app.get("/api/personas/{persona_id}/moments")
def list_moments_api(
    persona_id: int,
    before_id: int = 0,
    limit: int = 20,
    user: dict = Depends(current_user),
) -> dict:
    persona = _require_persona(user["id"], persona_id)
    settings = persona_settings.load(persona.get("settings"))
    size = min(max(int(limit or 20), 1), 50)
    items = store.list_moments(user["id"], persona_id, limit=size, before_id=before_id)
    return {
        "persona": {"id": persona["id"], "name": persona["name"]},
        "moments": [_moment_payload(item) for item in items],
        "moments_enabled": bool(settings["moments"].get("enabled")),
        "per_turn_cost": _moment_per_turn_cost(user["id"], persona),
        "next_before_id": items[-1]["id"] if len(items) >= size else 0,
    }


@app.post("/api/personas/{persona_id}/moments")
def publish_moment_api(persona_id: int, user: dict = Depends(current_user)) -> dict:
    """用户手动催更：立即生成并发布一条动态，受当日上限约束并按轮扣费。"""
    persona = _require_persona(user["id"], persona_id)
    settings = persona_settings.load(persona.get("settings"))
    cap = int(settings["moments"].get("max_per_day") or 2)
    if store.count_moments_since(persona_id, _moment_day_start_iso()) >= cap:
        raise HTTPException(status_code=429, detail=f"今天已经发满 {cap} 条了，明天再来吧")

    try:
        agent = get_agent(user["id"], persona)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=400, detail="分身数据缺失，请重新蒸馏人格") from None
    if not getattr(agent.config, "ready", False):
        raise HTTPException(status_code=400, detail="分身暂时无法使用，请检查模型配置")

    cost = _moment_cost_for(agent)
    charged = 0
    if cost > 0:
        try:
            store.deduct_credits(
                user["id"], cost, reason="朋友圈发布扣费", ref=f"persona:{persona_id}"
            )
            charged = cost
        except store.InsufficientCredits as error:
            raise HTTPException(
                status_code=402, detail=f"积分不足（当前 {error.balance}），暂时无法发布"
            ) from error

    def _fail(status: int, detail: str) -> None:
        if charged:
            _moment_refund(user["id"], charged, persona_id)
        raise HTTPException(status_code=status, detail=detail)

    stickers = store.list_stickers(user["id"], persona_id)
    memories = store.list_memories(user["id"], persona_id, limit=12)
    try:
        text, sticker_id = moments.compose(agent, settings, memories, stickers)
    except Exception:  # noqa: BLE001
        text, sticker_id = "", 0
    if not text:
        _fail(502, "生成失败，请稍后再试或检查模型配置")

    moment = store.add_moment(
        user["id"], persona_id, text, sticker_id=sticker_id, source="manual"
    )
    return {"moment": _moment_payload(moment)}


@app.delete("/api/moments/{moment_id}")
def delete_moment_api(moment_id: int, user: dict = Depends(current_user)) -> dict:
    if not store.delete_moment(user["id"], moment_id):
        raise HTTPException(status_code=404, detail="动态不存在")
    return {"ok": True}


@app.post("/api/moments/{moment_id}/like")
def like_moment_api(moment_id: int, user: dict = Depends(current_user)) -> dict:
    _require_moment(user, moment_id)
    store.set_moment_like(moment_id, user["id"], True)
    return {"liked": True, "like_count": store.count_moment_likes(moment_id)}


@app.delete("/api/moments/{moment_id}/like")
def unlike_moment_api(moment_id: int, user: dict = Depends(current_user)) -> dict:
    _require_moment(user, moment_id)
    store.set_moment_like(moment_id, user["id"], False)
    return {"liked": False, "like_count": store.count_moment_likes(moment_id)}


@app.get("/api/moments/{moment_id}/comments")
def list_moment_comments_api(
    moment_id: int, user: dict = Depends(current_user)
) -> dict:
    _require_moment(user, moment_id)
    return {"comments": store.list_moment_comments(moment_id)}


def _moment_agent_for(user: dict, moment: dict):
    persona = store.get_persona(user["id"], moment["persona_id"])
    if persona is None:
        return None
    try:
        return get_agent(user["id"], persona)
    except (FileNotFoundError, ValueError):
        return None


def _generate_moment_reply(user: dict, moment: dict, comment: dict, agent, charged: int) -> str:
    """生成分身对评论的回应；失败时退款并标记 failed，返回空串。"""
    reply = ""
    if agent is not None and getattr(agent.config, "ready", False):
        try:
            reply = (
                agent.reply(
                    comment["content"],
                    extra_context=_moment_reply_context(moment, comment["content"]),
                )
                or ""
            ).strip()
        except Exception:  # noqa: BLE001
            reply = ""
    if reply:
        store.set_moment_comment_reply(comment["id"], reply, "done")
        return reply
    store.set_moment_comment_reply(comment["id"], "", "failed")
    if charged:
        _moment_refund(user["id"], charged, moment["id"])
    return ""


def _charge_for_comment(user: dict, moment: dict, agent) -> int:
    cost = _moment_cost_for(agent)
    if cost <= 0:
        return 0
    try:
        store.deduct_credits(
            user["id"], cost, reason="朋友圈评论回复扣费", ref=f"moment:{moment['id']}"
        )
    except store.InsufficientCredits as error:
        raise HTTPException(
            status_code=402, detail=f"积分不足（当前 {error.balance}），暂时无法评论"
        ) from error
    return cost


@app.post("/api/moments/{moment_id}/comments")
def create_moment_comment_api(
    moment_id: int, payload: dict, user: dict = Depends(current_user)
) -> dict:
    moment = _require_moment(user, moment_id)
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="评论内容不能为空")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="评论内容过长")

    agent = _moment_agent_for(user, moment)
    charged = _charge_for_comment(user, moment, agent)
    comment = store.add_moment_comment(moment_id, user["id"], content)
    _generate_moment_reply(user, moment, comment, agent, charged)
    return {"comment": store.get_moment_comment(comment["id"])}


@app.post("/api/moments/comments/{comment_id}/retry")
def retry_moment_comment_api(comment_id: int, user: dict = Depends(current_user)) -> dict:
    comment = store.get_moment_comment(comment_id)
    if comment is None or int(comment.get("user_id") or 0) != int(user["id"]):
        raise HTTPException(status_code=404, detail="评论不存在")
    if comment.get("reply_status") == "done" and comment.get("reply"):
        return {"comment": comment}
    moment = store.get_moment(user["id"], comment["moment_id"])
    if moment is None:
        raise HTTPException(status_code=404, detail="动态不存在")

    agent = _moment_agent_for(user, moment)
    charged = _charge_for_comment(user, moment, agent)
    _generate_moment_reply(user, moment, comment, agent, charged)
    return {"comment": store.get_moment_comment(comment_id)}


@app.get("/api/moments/{moment_id}/sticker")
def moment_sticker_api(moment_id: int, user: dict = Depends(current_user)) -> FileResponse:
    moment = _require_moment(user, moment_id)
    sticker_id = int(moment.get("sticker_id") or 0)
    sticker = store.get_sticker(user["id"], sticker_id) if sticker_id else None
    if sticker is None:
        raise HTTPException(status_code=404, detail="配图不存在")
    path = Path(sticker["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已丢失")
    return FileResponse(path)


FAREWELL_PROMPT = (
    "这是你和对方在这个账号里的最后一段对话。请你以角色本人的身份，"
    "写一段真诚、克制、有温度的告别：谢谢对方这段时间的陪伴，说出现在的心情，"
    "也祝对方未来一切都好。像平时发微信一样分成几句话，不要用书面称呼，"
    "不要解释你在做什么，只输出要发给对方的内容本身。"
)


@app.get("/api/personas/{persona_id}/farewell")
def read_farewell(persona_id: int, user: dict = Depends(current_user)) -> dict:
    _require_persona(user["id"], persona_id)
    return {"farewell": store.get_farewell(user["id"], persona_id)}


@app.post("/api/personas/{persona_id}/farewell")
def create_farewell(persona_id: int, payload: dict | None = None, user: dict = Depends(current_user)) -> dict:
    """生成一封告别信：发送给对方，同时把人格置为「已告别」不再自动回复。"""
    persona = _require_persona(user["id"], persona_id)
    settings = persona_settings.load(persona.get("settings"))
    contact = str((payload or {}).get("contact") or persona.get("last_contact") or "").strip()
    note = str((payload or {}).get("note") or "").strip()
    prompt = FAREWELL_PROMPT + (f"\n\n可以提到：{note}" if note else "")
    try:
        agent = get_agent(user["id"], persona)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not agent.config.ready:
        raise HTTPException(status_code=400, detail="尚未配置大模型，暂时无法生成告别信")
    try:
        _record_platform_call(user["id"], agent.config)
    except PlatformCapped as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    try:
        text = agent.reply(prompt, temperature=persona_settings.temperature(settings))
    except LLMError as error:
        raise HTTPException(status_code=400, detail=f"生成告别信失败：{error}") from error
    text = _clean_outbound(text)
    if not text:
        raise HTTPException(status_code=400, detail="告别信生成失败，请稍后重试")
    saved = store.save_farewell(user["id"], persona_id, contact, text)
    store.update_persona(user["id"], persona_id, status="retired", is_active=0)
    _agents.pop(f"{user['id']}:{persona_id}", None)
    delivered = False
    if contact:
        delivered = _proactive_send(user["id"], contact, text, persona=persona)
        if delivered:
            store.add_turn(user["id"], persona_id, "assistant", text, contact=contact)
    observability.METRICS.inc("farewell.created")
    return {"farewell": saved, "delivered": delivered}


@app.post("/api/personas/{persona_id}/feedback")
def submit_feedback(
    persona_id: int, payload: dict, user: dict = Depends(current_user)
) -> dict:
    _require_persona(user["id"], persona_id)
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="反馈内容不能为空")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="反馈内容过长")
    item = store.add_feedback(
        user["id"], persona_id, content, contact=str(payload.get("contact") or "")
    )
    return {"ok": True, "id": item["id"]}


@app.patch("/api/personas/{persona_id}/contacts/{contact_key}")
def rename_contact(
    persona_id: int, contact_key: str, payload: dict, user: dict = Depends(current_user)
) -> dict:
    _require_persona(user["id"], persona_id)
    name = str(payload.get("name") or "").strip()[:40]
    if not store.set_contact_name(user["id"], persona_id, contact_key, name):
        raise HTTPException(status_code=404, detail="联系人不存在")
    return {"name": name}


@app.post("/api/personas/{persona_id}/debug")
def debug_persona(
    persona_id: int, payload: dict, user: dict = Depends(current_user)
) -> dict:
    """在调试面板里试跑一次：返回上下文、召回记忆、系统提示与模型回复，不写入记录。"""
    persona = _require_persona(user["id"], persona_id)
    settings = persona_settings.load(persona.get("settings"))
    contact = str(payload.get("contact") or persona.get("last_contact") or "").strip()
    message = str(payload.get("message") or "").strip()

    # 历史按 contact 过滤即已实现「每个聊天对象各自独立上下文」，不能因为开了
    # 独立会话就把当前对话的历史清零，否则人格不记得自己刚说过什么。
    limit = persona_settings.memory_limit(settings)
    stored = (
        store.list_turns(user["id"], persona_id, limit=limit, contact=contact)
        if limit and contact
        else []
    )
    history = history_for_llm(stored, settings)

    long_term = store.list_memories(user["id"], persona_id, contact) if contact else []
    hits = memories.recall(long_term, message) if message else []
    extra_context = _with_tuning(settings, memories.format_block(hits, persona["name"]))
    if stored:
        gap_hint = clock.gap_hint(stored[-1].get("created_at"))
        if gap_hint:
            extra_context = f"{extra_context}\n\n{gap_hint}".strip() if extra_context else gap_hint

    result = {
        "contact": contact,
        "ready": False,
        "error": "",
        "provider": settings["model"].get("provider") or "system",
        "model": "",
        "base_url": "",
        "memory_mode": settings["model"].get("memory_mode"),
        "long_term_memory": bool(settings["model"].get("long_term_memory", True)),
        "extract_every": int(settings["model"].get("memory_extract_every") or 6),
        "memory_total": len(long_term),
        "memory_hits": hits,
        "extra_context": extra_context,
        "history_count": len(history),
        "history": history[-12:],
        "channel": _channel_info(user["id"], persona)["wechat"],
        "silence_allowed": bool(settings["advanced"].get("allow_silence")),
        "system_prompt": "",
        "reply": "",
        "silenced": False,
    }

    try:
        agent = get_agent(user["id"], persona)
    except (FileNotFoundError, ValueError) as error:
        result["error"] = str(error)
        return result
    result["model"] = agent.config.model
    result["base_url"] = agent.config.base_url or ""
    result["ready"] = bool(agent.config.ready)
    if not result["ready"]:
        result["error"] = "尚未配置大模型 API Key"
        return result

    preview_query = message or (history[-1]["content"] if history else "你好")
    result["system_prompt"] = agent.build_system(preview_query, extra_context)
    if not message:
        return result

    message_for_llm = message
    if settings["advanced"].get("allow_silence"):
        message_for_llm = message + "\n\n（如果此刻不想回复，只输出 [[SILENCE]]）"
    try:
        _record_platform_call(user["id"], agent.config)
    except PlatformCapped as error:
        result["error"] = f"模型调用失败：{error}"
        return result
    try:
        reply = agent.reply(
            message_for_llm,
            history=history,
            temperature=persona_settings.temperature(settings),
            extra_context=extra_context,
        )
    except Exception as error:  # noqa: BLE001 - 调试面板需展示友好错误
        result["error"] = f"模型调用失败：{error}"
        return result
    result["silenced"] = "[[SILENCE]]" in reply
    result["reply"] = "" if result["silenced"] else reply
    return result


@app.post("/api/personas/{persona_id}/proactive/run")
def run_proactive(persona_id: int, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], persona_id)
    if not persona.get("last_contact"):
        raise HTTPException(status_code=400, detail="尚未记录聊天对象，请先在消息渠道填写对方 ID")
    try:
        agent = get_agent(user["id"], persona)
        settings = persona_settings.load(persona.get("settings"))
        prompt = (settings["proactive"].get("prompt") or "").strip() or scheduler.DEFAULT_PROMPT
        if not agent.config.ready:
            raise HTTPException(status_code=400, detail="尚未配置大模型 API Key")
        try:
            _record_platform_call(user["id"], agent.config)
        except PlatformCapped as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        text = agent.reply(prompt)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    if not text:
        return {"text": "", "delivered": False}
    delivered = _proactive_send(user["id"], persona["last_contact"], text, persona=persona)
    if delivered:
        store.add_turn(user["id"], persona_id, "assistant", text, contact=persona["last_contact"] or "")
    return {"text": text, "delivered": delivered}


@app.post("/api/personas/{persona_id}/activate")
def activate(persona_id: int, user: dict = Depends(current_user)) -> dict:
    persona = store.activate_persona(user["id"], persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="人格不存在")
    if persona.get("status") == "retired":
        # 重新激活已告别的人格时恢复状态，否则主动消息调度不会再触发。
        store.update_persona(user["id"], persona_id, status="ready")
        persona = store.get_persona(user["id"], persona_id) or persona
    binding = store.get_wechat_binding(user["id"])
    if binding is not None:
        store.set_binding_persona(user["id"], persona_id)
    return persona


@app.get("/api/skill")
def skill(persona_id: int | None = None, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], persona_id)
    path = Path(persona["dir"]) / "SKILL.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="尚未生成人格")
    return {"skill": path.read_text(encoding="utf-8"), "persona": persona}


# --------------------------------------------------------------------------- #
# 素材与蒸馏
# --------------------------------------------------------------------------- #

def _material_batch(user_id: int) -> Path:
    """复用上一批素材目录。

    「上传文件」与「加入粘贴内容」在界面上是同一份素材的两种提供方式，但后端原先
    各自 new_batch 并覆盖 _last_batch，导致后提交的一份把先提交的一份顶掉。改成
    累积到同一批，直到解析完成才重置。
    """
    existing = _last_batch.get(user_id)
    if existing:
        path = Path(existing)
        if path.exists():
            return path
    batch = Path(workspace.new_batch(user_id, "material"))
    _last_batch[user_id] = str(batch)
    return batch


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), user: dict = Depends(current_user)) -> dict:
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"单次最多上传 {MAX_FILES} 个文件")
    workspace.ensure_user(user["id"])
    batch = _material_batch(user["id"])
    saved: list[str] = []
    for item in files:
        data = await item.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"文件过大：{item.filename}")
        destination = workspace.safe_join(batch, item.filename or "chat.txt")
        destination.write_bytes(data)
        saved.append(destination.name)
    return {"saved": saved, "count": len(saved)}


@app.post("/api/paste")
def paste(payload: PasteRequest, user: dict = Depends(current_user)) -> dict:
    content = (payload.text or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="粘贴内容为空")
    workspace.ensure_user(user["id"])
    batch = _material_batch(user["id"])
    (batch / "pasted.txt").write_text(content, encoding="utf-8")
    return {"chars": len(content)}


@app.post("/api/ingest")
def api_ingest(payload: IngestRequest, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], payload.persona_id)
    source = _last_batch.get(user["id"])
    if source is None or not Path(source).exists():
        raw = workspace.raw_dir(user["id"])
        if not raw.exists() or not any(raw.iterdir()):
            raise HTTPException(status_code=400, detail="请先上传或粘贴素材")
        source = raw
    target = (payload.target or persona.get("target_name") or None)
    try:
        result = ingest.run(Path(source), Path(persona["dir"]), target)
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    store.update_persona(
        user["id"], persona["id"], target_name=result.target, status="ingested"
    )
    # 素材已解析进人格目录，清掉暂存批，避免后续解析重复使用旧素材。
    _last_batch.pop(user["id"], None)
    return {"total": len(result.messages), "target": result.target, "persona_id": persona["id"]}


@app.post("/api/distill")
def api_distill(payload: DistillRequest, user: dict = Depends(current_user)) -> dict:
    persona = _require_persona(user["id"], payload.persona_id)
    config_obj = build_config(user["id"], persona)
    if payload.use_llm:
        try:
            _record_platform_call(user["id"], config_obj)
        except PlatformCapped as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
    try:
        result = distill.run(
            Path(persona["dir"]), config=config_obj, use_llm=payload.use_llm
        )
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    _agents.pop(f"{user['id']}:{persona['id']}", None)
    store.update_persona(user["id"], persona["id"], status="ready")
    return result


@app.get("/api/export")
def export_persona(persona_id: int | None = None, user: dict = Depends(current_user)):
    persona = _require_persona(user["id"], persona_id)
    directory = Path(persona["dir"])
    if not directory.exists():
        raise HTTPException(status_code=400, detail="人格目录不存在")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, str(path.relative_to(directory)))
    buffer.seek(0)
    name = persona["name"] or "persona"
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "persona"
    quoted = urllib.parse.quote(f"{name}.zip")
    disposition = (
        f"attachment; filename=\"{ascii_name}.zip\"; filename*=UTF-8''{quoted}"
    )
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


# --------------------------------------------------------------------------- #
# 模型配置 API
# --------------------------------------------------------------------------- #

@app.get("/api/model-config")
def get_model_config(user: dict = Depends(current_user)) -> dict:
    row = store.get_model_config(user["id"])
    if row is None:
        return {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                "has_key": False, "api_key_masked": "", "key_status": "", "key_error": ""}
    return {
        "base_url": row.get("base_url") or "https://api.deepseek.com/v1",
        "model": row.get("model") or "deepseek-chat",
        "has_key": bool(row.get("api_key_encrypted")),
        "api_key_masked": crypto.mask(crypto.decrypt(row.get("api_key_encrypted"))),
        "key_status": row.get("key_status") or "",
        "key_error": row.get("key_error") or "",
    }


@app.put("/api/model-config")
def set_model_config(payload: ModelConfigRequest, user: dict = Depends(current_user)) -> dict:
    existing = store.get_model_config(user["id"]) or {}
    api_key_encrypted = existing.get("api_key_encrypted") or ""
    if payload.api_key is not None and payload.api_key.strip():
        api_key_encrypted = crypto.encrypt(payload.api_key.strip())
    base_url = _validate_base_url(
        payload.base_url or existing.get("base_url") or "https://api.deepseek.com/v1"
    )
    model = (payload.model or existing.get("model") or "deepseek-chat").strip()
    store.set_model_config(user["id"], api_key_encrypted, base_url, model)
    _agents.clear()
    return get_model_config(user)


@app.get("/api/credits")
def get_credits(user: dict = Depends(current_user)) -> dict:
    platform = load_platform_config()
    coins = store.coins_summary(user["id"])
    summary = store.credits_summary(user["id"])
    tickets = store.distill_tickets_summary(user["id"])
    return {
        "balance": summary["balance"],
        "summary": summary,
        "ratio": summary["ratio"],
        "remaining_ratio": summary["remaining_ratio"],
        "expiring_soon": summary["expiring_soon"],
        "next_expiry": summary["next_expiry"],
        "expired": summary["expired"],
        "coins": coins["balance"],
        "coins_summary": coins,
        "per_turn_cost": platform.per_turn_cost,
        "platform_ready": platform.ready,
        "platform_model": platform.model,
        "packages": store.list_credit_packages(active_only=True),
        "batches": store.list_credit_batches(user["id"], limit=20, live_only=True),
        "ledger": store.list_credit_ledger(user["id"], limit=20),
        "coin_ledger": store.list_coin_ledger(user["id"], limit=20),
        "distill_tickets": tickets,
        "distill_ticket_ledger": store.list_distill_ticket_ledger(user["id"], limit=20),
    }


class DistillTicketPurchase(BaseModel):
    quantity: int = 1
    client_id: str | None = None


@app.post("/api/distill-tickets/purchase")
def purchase_distill_tickets(
    payload: DistillTicketPurchase,
    user: dict = Depends(current_user),
    idempotency_key: str = Header("", alias="Idempotency-Key"),
) -> dict:
    idem = (payload.client_id or idempotency_key).strip()[:64]
    quantity = max(int(payload.quantity or 1), 1)
    quantity = min(quantity, 100)
    try:
        result = store.purchase_distill_ticket(user["id"], quantity, idem=idem)
    except store.InsufficientCoins as error:
        raise HTTPException(
            status_code=402, detail=f"念念币不足，当前 {error.balance} 念念币"
        ) from error
    except KeyError as error:
        raise HTTPException(status_code=400, detail=str(error.args[0])) from error
    if not result.get("duplicate"):
        _audit(user, "distill_ticket.purchase", detail=f"qty={quantity} spent={result['spent']}")
    return {
        "ok": True,
        "coins": result["coins"],
        "distill_tickets": store.distill_tickets_summary(user["id"]),
        "coins_summary": store.coins_summary(user["id"]),
        "duplicate": bool(result.get("duplicate")),
        "spent": result["spent"],
    }


@app.post("/api/redeem")
def redeem_code(payload: RedeemRequest, user: dict = Depends(current_user)) -> dict:
    key = f"redeem:{user['id']}"
    if _auth_blocked(key):
        raise HTTPException(
            status_code=429,
            detail="尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)},
        )
    try:
        result = store.redeem_code(user["id"], payload.code)
    except store.RedemptionError as error:
        _auth_note(key)
        # 统一错误文案，避免暴露“存在但已使用”的探测信号。
        raise HTTPException(status_code=400, detail="兑换码无效或已被使用") from error
    _auth_clear(key)
    _audit(user, "coins.redeem", detail=f"amount={result['amount']}")
    return {"ok": True, "coins": result["coins"], "amount": result["amount"],
            "summary": store.coins_summary(user["id"])}


@app.post("/api/packages/{package_id}/purchase")
def purchase_package(
    package_id: int,
    user: dict = Depends(current_user),
    idempotency_key: str = Header("", alias="Idempotency-Key"),
) -> dict:
    idem = idempotency_key.strip()[:64]
    try:
        result = store.purchase_package(user["id"], package_id, idem=idem)
    except store.InsufficientCoins as error:
        raise HTTPException(status_code=400, detail=f"念念币不足，当前 {error.balance} 念念币") from error
    except KeyError as error:
        raise HTTPException(status_code=400, detail=str(error.args[0])) from error
    duplicate = bool(result.get("duplicate"))
    if not duplicate:
        _audit(user, "package.purchase", target=str(package_id),
               detail=f"spent={result['spent']}")
    return {"ok": True, "coins": result["coins"], "credits": result["credits"],
            "duplicate": duplicate,
            "summary": store.credits_summary(user["id"]),
            "coins_summary": store.coins_summary(user["id"])}


# --------------------------------------------------------------------------- #
# 微信 API
# --------------------------------------------------------------------------- #

@app.get("/api/wechat/status")
def wechat_status(verbose: bool = False, user: dict = Depends(current_user)) -> dict:
    binding = store.get_wechat_binding(user["id"])
    if binding is None:
        return {"available": bool(wechat.WECLAW_BIN), "phase": "idle", "running": False}
    try:
        bridge = _bridge(user["id"])
        # 仅当用户希望转发开启时才自动拉起，避免打开面板就把已关闭的渠道重新点亮。
        if _wants_forwarding(binding):
            bridge.maybe_autostart()
        status = bridge.status(with_svg=verbose)
    except HTTPException as error:
        return {"available": False, "phase": "unavailable", "running": False,
                "detail": error.detail}
    if status.get("running"):
        store.set_binding_phase(user["id"], "running")
    elif not status.get("login_alive") and status.get("phase") == "idle":
        # 服务重启后内存中的 phase 会回到 idle；沿用已落库的状态，避免把
        # 「已登录待转发/登录已失效」显示成「未登录」。
        persisted = binding.get("phase") or ""
        if persisted in ("logged-in", "expired"):
            status["phase"] = persisted
            status["expired"] = persisted == "expired"
    if not verbose:
        for key in ("qr_svg", "qr_error", "log", "bridge_log", "weclaw",
                    "config_path", "home_dir"):
            status.pop(key, None)
    return status


@app.post("/api/wechat/login")
def wechat_start_login(user: dict = Depends(current_user)) -> dict:
    if not wechat.WECLAW_BIN:
        raise HTTPException(status_code=400, detail="未找到 weclaw，请先安装")
    _ensure_binding(user["id"])
    return _bridge(user["id"]).start_login(_ensure_binding(user["id"])["bridge_token"])


@app.post("/api/wechat/start")
def wechat_start(user: dict = Depends(current_user)) -> dict:
    if not wechat.WECLAW_BIN:
        raise HTTPException(status_code=400, detail="未找到 weclaw")
    result = _bridge(user["id"]).start_bridge()
    store.set_binding_phase(user["id"], "running" if result.get("code") == 0 else "failed")
    return result


@app.post("/api/wechat/stop")
def wechat_stop(user: dict = Depends(current_user)) -> dict:
    result = manager.stop(user["id"])
    store.set_binding_phase(user["id"], "logged-in")
    return result


@app.post("/api/wechat/rotate-token")
def wechat_rotate_token(user: dict = Depends(current_user)) -> dict:
    """轮换桥接令牌：旧令牌立即失效，并让桥接进程加载新配置。"""
    binding = _ensure_binding(user["id"])
    if not wechat.WECLAW_BIN:
        raise HTTPException(status_code=400, detail="未找到 weclaw")
    manager.stop(user["id"], explicit=False)
    new_token = crypto.new_bridge_token(user["id"])
    store.set_binding_token(user["id"], new_token)
    bridge = manager.get(user["id"], workspace.home_dir(user["id"]), new_token)
    if bridge.is_bound():
        bridge.start_bridge()
        store.set_binding_phase(user["id"], "running")
    else:
        store.set_binding_phase(user["id"], binding.get("phase") or "idle")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 长期记忆抽取
# --------------------------------------------------------------------------- #

def _memory_lock(user_id: int, persona_id: int, contact: str) -> threading.Lock:
    key = (user_id, persona_id, contact)
    with _memory_locks_lock:
        lock = _memory_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _memory_locks[key] = lock
        return lock


def _extract_task(user_id: int, persona_id: int, contact: str) -> None:
    """后台抽取长期记忆；同一联系人串行执行，失败时保留轮次待重试。"""
    if not contact:
        return
    lock = _memory_lock(user_id, persona_id, contact)
    if not lock.acquire(blocking=False):
        return
    try:
        persona = store.get_persona(user_id, persona_id)
        if persona is None:
            return
        settings = persona_settings.load(persona.get("settings"))
        if not settings["model"].get("long_term_memory", True):
            return
        row = store.get_contact(user_id, persona_id, contact)
        after = int(row.get("last_turn_id") or 0) if row else 0
        turns = store.list_turns_after(user_id, persona_id, contact, after)
        if len(turns) < int(settings["model"].get("memory_extract_every") or 6):
            return
        try:
            agent = get_agent(user_id, persona)
        except (FileNotFoundError, ValueError):
            return
        existing = [
            item["content"]
            for item in store.list_memories(user_id, persona_id, contact)
        ]
        fresh = memories.extract(agent.config, persona["name"], turns, existing)
        if fresh is None:
            return
        for item in fresh:
            store.add_memory(user_id, persona_id, contact, item["kind"], item["content"])
        store.set_contact_last_turn(user_id, persona_id, contact, turns[-1]["id"])
    except Exception:
        return
    finally:
        lock.release()


def _start_extract(user_id: int, persona: dict, contact: str) -> None:
    thread = threading.Thread(
        target=_extract_task,
        args=(user_id, persona["id"], contact),
        name=f"memory-{persona['id']}-{contact[:8]}",
        daemon=True,
    )
    thread.start()


# --------------------------------------------------------------------------- #
# 微信消息路由
# --------------------------------------------------------------------------- #

def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def _completion(request: OAIRequest, reply: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model or "persona",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": reply},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _rate_limited(token: str) -> bool:
    """滑动窗口限流：每个令牌每分钟最多 RATE_LIMIT_PER_MINUTE 次。"""
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(token, [])
        bucket[:] = [stamp for stamp in bucket if now - stamp < 60]
        if len(bucket) >= RATE_LIMIT_PER_MINUTE:
            return True
        bucket.append(now)
        if len(_rate_buckets) > 2000:
            for old in list(_rate_buckets)[:1000]:
                if old != token:
                    _rate_buckets.pop(old, None)
        return False


def _reply_lock(key: tuple) -> threading.Lock:
    with _reply_locks_guard:
        lock = _reply_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _reply_locks[key] = lock
        return lock


@app.post("/v1/chat/completions/{bridge_token}")
def route_chat(bridge_token: str, request: OAIRequest) -> dict:
    if crypto.signed_bridge_token(bridge_token) and not crypto.verify_bridge_token(bridge_token):
        raise HTTPException(status_code=401, detail="桥接令牌校验失败")
    if _rate_limited(bridge_token):
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": "60"},
        )
    binding = store.get_binding_by_token(bridge_token)
    if binding is None:
        raise HTTPException(status_code=404, detail="无效的桥接令牌")
    user_id = binding["user_id"]
    owner = store.get_user(user_id)
    if owner is None or (owner.get("status") or "active") != "active":
        # 账号已停用或注销：静默不回复，数据保留但不允许该人格继续对外发言。
        observability.METRICS.inc("reply.account_disabled")
        return _completion(request, "")
    persona = store.get_persona(user_id, binding["persona_id"]) if binding.get("persona_id") else None
    persona = persona or store.get_active_persona(user_id)
    if persona is None:
        raise HTTPException(status_code=400, detail="该用户尚未创建人格")
    if persona.get("status") == "retired":
        # 已「告别」的人格不再自动回复；用户在应用里重新激活后会恢复。
        observability.METRICS.inc("reply.retired")
        return _completion(request, "")

    settings = persona_settings.load(persona.get("settings"))
    contact = (request.user or "").strip()
    if contact:
        store.update_persona(user_id, persona["id"], last_contact=contact)
        store.touch_contact(user_id, persona["id"], contact)

    turns = [
        {"role": turn.role, "content": _text_of(turn.content)}
        for turn in request.messages
        if turn.role in ("user", "assistant") and _text_of(turn.content)
    ]
    user_turns = [turn for turn in turns if turn["role"] == "user"]
    if not user_turns:
        raise HTTPException(status_code=400, detail="messages 中缺少 user 消息")
    message = user_turns[-1]["content"]

    # 情绪危机优先于一切：命中自伤/极端情绪时跳过角色扮演，直接给出确定性
    # 的求助资源，并提醒运营者关注，避免大模型在危机语境下继续对话。
    if settings["safety"].get("crisis_support", True) and safety.detect_crisis(message):
        observability.METRICS.inc("reply.crisis")
        _notify(user_id, "crisis", "有用户表达了自伤/极端情绪，请及时关注并联系专业帮助。")
        _notify_admins("crisis", f"用户 #{user_id} 触发了情感安全兜底，请及时关注。")
        return _completion(request, safety.crisis_reply(persona["name"]))

    # 收到图片时的占位文本：未开启回应则直接静默，不调用大模型。
    if message.strip() == IMAGE_PLACEHOLDER and not settings["advanced"].get("reply_to_images"):
        observability.METRICS.inc("reply.image_ignored")
        return _completion(request, "")

    memory_limit = persona_settings.memory_limit(settings)
    # 只有拿到明确的联系人（request.user）才回捞历史，否则会串到该人格其他
    # 联系人的对话。拿不到联系人时退回本次请求携带的上下文。
    stored = (
        store.list_turns(user_id, persona["id"], limit=memory_limit, contact=contact)
        if memory_limit and contact
        else []
    )
    if stored:
        history = history_for_llm(stored, settings)
    else:
        history = turns[: turns.index(user_turns[-1])][-memory_limit:] if memory_limit else []

    extra_context = ""
    if contact and settings["model"].get("long_term_memory", True):
        long_term = store.list_memories(user_id, persona["id"], contact, limit=MEMORY_SCAN_LIMIT)
        extra_context = memories.format_block(
            memories.recall(long_term, message, k=6), persona["name"]
        )
    stickers = store.list_stickers(user_id, persona["id"])
    media_hint = media_reply.build_hint(stickers, settings)
    if media_hint:
        extra_context = f"{extra_context}\n\n{media_hint}".strip() if extra_context else media_hint
    extra_context = _with_tuning(settings, extra_context)

    # 让分身知道距离对方上一次说话过了多久，避免「隔了三天也当刚聊完」。
    if stored:
        gap_hint = clock.gap_hint(stored[-1].get("created_at"))
        if gap_hint:
            extra_context = f"{extra_context}\n\n{gap_hint}".strip() if extra_context else gap_hint

    try:
        agent = get_agent(user_id, persona)
    except (FileNotFoundError, ValueError):
        observability.METRICS.inc("reply.agent_missing")
        _notify(user_id, "persona", "分身数据缺失，暂时无法回复，请重新蒸馏人格。")
        _notify_admins("persona", f"用户 #{user_id} 的分身数据缺失，无法回复，需要重新蒸馏。")
        return _completion(request, REPLY_UNAVAILABLE)
    if not agent.config.ready:
        # 直接以正常回复告知用户，避免非 200 让 weclaw 静默丢弃；同时提醒运营者。
        observability.METRICS.inc("reply.platform_unavailable")
        _notify(user_id, "platform", "平台模型尚未启用，分身暂时无法回复，请尽快配置。")
        _notify_admins("platform", "平台模型尚未启用，多名用户的分身无法回复，请尽快配置。")
        return _completion(request, REPLY_UNAVAILABLE)

    is_platform = bool(getattr(agent.config, "platform", False))
    platform_cfg = load_platform_config() if is_platform else None
    per_turn_cost = int(platform_cfg.per_turn_cost) if platform_cfg else 0

    message_for_llm = message
    if settings["advanced"].get("allow_silence"):
        message_for_llm = message + "\n\n（如果此刻不想回复，只输出 [[SILENCE]]）"
    temperature = (
        request.temperature if request.temperature is not None
        else persona_settings.temperature(settings)
    )

    merge_seconds = float(settings["advanced"].get("merge_seconds") or 0)
    dedup_seconds = max(merge_seconds, DEDUP_MIN_SECONDS)
    key = (persona["id"], contact, message)
    now = time.time()
    allowed_kinds = media_reply.enabled_kinds(settings)
    media_directives: list[dict] = []
    insufficient = False
    with _reply_lock(key):
        cached = _reply_cache.get(key)
        fresh = not (dedup_seconds > 0 and cached and now - cached[0] < dedup_seconds)
        if not fresh:
            reply = cached[1]
        else:
            charged = 0
            # 命中缓存的重复消息不消耗积分，也不占用平台日额度，因此计费与额度校验
            # 都放在真正需要调用大模型的分支内。
            if is_platform:
                platform_limit = platform_cfg.daily_limit
                if (
                    platform_limit
                    and store.count_platform_calls_since(user_id, _day_start_iso()) >= platform_limit
                ):
                    observability.METRICS.inc("reply.platform_capped")
                    _notify(user_id, "platform_cap", "今日平台模型额度已用完，分身已暂停回复。")
                    _notify_admins("platform_cap", "有用户触达今日平台模型额度上限，可考虑调整额度。")
                    return _completion(request, REPLY_PLATFORM_CAPPED)
                if per_turn_cost > 0:
                    # 先原子扣费再调用大模型，避免并发下同一余额被多次通过校验后白嫖回复。
                    try:
                        store.deduct_credits(
                            user_id, per_turn_cost, reason="聊天回复扣费",
                            ref=f"persona:{persona['id']}",
                        )
                        charged = per_turn_cost
                    except store.InsufficientCredits:
                        observability.METRICS.inc("reply.insufficient_credits")
                        insufficient = True
            if insufficient:
                _notify(user_id, "credits", "积分已用完，分身暂停回复；去设置页兑换念念币或购买积分套餐可继续。")
                reply = REPLY_NO_CREDITS
            else:
                try:
                    if is_platform:
                        store.add_platform_call(user_id)
                    reply = agent.reply(
                        message_for_llm,
                        history=history,
                        temperature=temperature,
                        extra_context=extra_context,
                    )
                except LLMError as error:
                    if charged:
                        try:
                            store.grant_credits(
                                user_id, charged, reason="回复失败退款", actor="system",
                                ref=f"persona:{persona['id']}",
                            )
                        except Exception:  # noqa: BLE001
                            log.warning("refund failed", extra={"event": "reply.refund_fail"})
                    observability.METRICS.inc("reply.error")
                    if error.auth and not is_platform:
                        store.set_model_key_status(user_id, "invalid", str(error))
                    log.warning(
                        "llm call failed",
                        extra={
                            "event": "reply.llm_error",
                            "user_id": user_id,
                            "auth": bool(error.auth),
                            "error": str(error),
                        },
                    )
                    detail = (
                        "大模型 Key 无效或已过期，请重新配置"
                        if error.auth
                        else "模型服务暂时不可用"
                    )
                    _notify(user_id, "llm", f"{detail}，分身暂时无法回复。")
                    _notify_admins("llm", f"用户 #{user_id} 的模型调用失败：{detail}。")
                    # 以正常回复返回，避免 non-200 被 weclaw 静默丢弃；扣费已在上方退回。
                    return _completion(request, REPLY_MODEL_ERROR)
                # 只有真正用自带 Key 调模型时才更新它的状态；走平台内置模型时
                # 成败与用户的 Key 无关，不能把状态覆盖成 ok / invalid。
                if not is_platform:
                    error = getattr(agent, "last_error", None)
                    if error is None:
                        store.set_model_key_status(user_id, "ok", "")
                    elif getattr(error, "auth", False):
                        store.set_model_key_status(user_id, "invalid", str(error))
        if "[[SILENCE]]" in reply:
            reply = ""
            observability.METRICS.inc("reply.silence")
        reply, media_directives = media_reply.parse_reply(reply, allowed_kinds)
        reply = _clean_outbound(reply)
        reply = _apply_filter(reply, settings)
        capped = False
        if reply and not _quota_ok(user_id, settings):
            observability.METRICS.inc("reply.capped")
            _notify(user_id, "quota", "发送频率达到上限，部分回复已暂缓，请检查微信是否正常。")
            reply = ""
            capped = True
        if fresh and not insufficient:
            if getattr(agent, "last_fallback", False):
                # 兜底话术只是过渡语，不是分身的真实回复：不写入历史（否则模型会
                # 把它当范例复读），不进缓存，并退回本轮扣费。
                observability.METRICS.inc("reply.fallback")
                if charged:
                    try:
                        store.grant_credits(
                            user_id, charged, reason="回复降级退款", actor="system",
                            ref=f"persona:{persona['id']}",
                        )
                    except Exception:  # noqa: BLE001
                        log.warning("refund failed", extra={"event": "reply.refund_fail"})
                _notify(user_id, "llm", "分身这次没能回上话，本轮积分已退回。")
                store.add_turn(user_id, persona["id"], "user", message, contact=contact)
            else:
                store.add_turn(user_id, persona["id"], "user", message, contact=contact)
                if reply:
                    observability.METRICS.inc("reply.ok")
                    if dedup_seconds > 0:
                        _reply_cache[key] = (now, reply)
                        if len(_reply_cache) > 500:
                            for old in sorted(_reply_cache, key=lambda k: _reply_cache[k][0])[:250]:
                                _reply_cache.pop(old, None)
                    store.add_turn(user_id, persona["id"], "assistant", reply, contact=contact)
                    if contact:
                        _start_extract(user_id, persona, contact)
                else:
                    # 没有可发送的内容（乱码丢弃 / 频率上限 / 静默）：不算一次成功
                    # 回复，退回本轮扣费，也不能把空回复写进缓存。
                    observability.METRICS.inc("reply.empty")
                    if charged:
                        try:
                            store.grant_credits(
                                user_id, charged, reason="无回复退款", actor="system",
                                ref=f"persona:{persona['id']}",
                            )
                        except Exception:  # noqa: BLE001
                            log.warning("refund failed", extra={"event": "reply.refund_fail"})
                    if not capped:
                        _notify(user_id, "llm", "分身这次没发出内容，本轮积分已退回。")

    if insufficient:
        return _completion(request, reply)

    # 真人感：把长回复拆成几条短消息，首条随本次响应返回，其余按打字节奏入队。
    # 先算出首条的拟人等待时间，作为后续分段的排队基准，保证到达顺序与生成顺序一致。
    lead_delay = _reply_delay_seconds(reply, settings) if reply else 0.0
    if (
        fresh
        and contact
        and reply
        and settings["advanced"].get("split_replies", True)
        and not media_directives
    ):
        segments = humanize.split_reply(reply, settings["advanced"].get("max_segments", 3))
        if len(segments) > 1:
            reply = segments[0]
            _queue_text_segments(user_id, persona, contact, segments[1:], base_delay=lead_delay)

    if fresh and contact:
        _queue_media_replies(user_id, persona, contact, stickers, media_directives)

    if reply:
        _record_send(user_id)
        if lead_delay > 0:
            observability.METRICS.inc("reply.delayed")
            time.sleep(lead_delay)

    return _completion(request, reply)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "users": len(store.list_users())}


@app.get("/api/metrics")
def metrics(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看指标")
    data = observability.snapshot()
    data["outbox"] = store.outbox_stats()
    return data


@app.get("/api/notifications")
def list_notifications(user: dict = Depends(current_user)) -> dict:
    return {
        "unread": store.count_unread_alerts(user["id"]),
        "items": store.list_alerts(user["id"], limit=20),
    }


@app.post("/api/notifications/read")
def read_notifications(user: dict = Depends(current_user)) -> dict:
    store.mark_alerts_read(user["id"])
    return {"ok": True, "unread": 0}


@app.post("/api/reports")
def create_report(payload: ReportRequest, user: dict = Depends(current_user)) -> dict:
    category = (payload.category or "other").strip()[:40] or "other"
    detail = (payload.detail or "").strip()[:2000]
    persona_id = payload.persona_id
    if persona_id is not None and store.get_persona(user["id"], persona_id) is None:
        persona_id = None
    report = store.add_report(user["id"], category, detail, persona_id)
    log.info(
        "user report submitted",
        extra={"event": "report.create", "user_id": user["id"], "report_id": report.get("id")},
    )
    # 每条举报都不同，不做同类冷却。
    _notify_admins("report", f"新的举报 #{report.get('id')}：{category}", cooldown=0)
    return {"ok": True, "id": report.get("id")}


@app.get("/api/admin/summary")
def admin_summary(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    return {
        "users": store.count_users(),
        "personas": store.count_personas(),
        "reports": store.count_reports(status=""),
        "open_reports": store.count_reports(status="open"),
        "platform_usage": store.platform_usage_stats(_day_start_iso()),
        "outbox": store.outbox_stats(),
        "credits": store.credits_totals(),
        "coins": store.coins_totals(),
        "redemption": store.redemption_stats(),
        "insights": store.admin_insights(),
        "alerts_unread": store.count_unread_alerts(user["id"]),
        "alerts": store.list_alerts(user["id"], limit=30, unread_only=True),
    }


@app.get("/api/admin/users")
def admin_users(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    rows = store.list_users()
    return {
        "items": [
            {
                "id": row["id"],
                "username": row["username"],
                "role": row.get("role") or "user",
                "status": row.get("status") or "active",
                "created_at": row.get("created_at"),
                "personas": store.count_personas_for_user(row["id"]),
                "credits": int(row.get("credits") or 0),
                "coins": int(row.get("coins") or 0),
            }
            for row in rows
        ]
    }


@app.get("/api/admin/reports")
def admin_reports(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    return {"items": store.list_reports(limit=100)}


@app.post("/api/admin/reports/{report_id}/resolve")
def admin_resolve_report(report_id: int, user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    store.set_report_status(report_id, "resolved")
    _audit(user, "report.resolve", target=str(report_id))
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    return {"items": store.list_audit(limit=100)}


@app.get("/api/admin/alerts")
def admin_alerts(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    return {
        "items": store.list_alerts(user["id"], limit=60),
        "unread": store.count_unread_alerts(user["id"]),
    }


@app.post("/api/admin/alerts/read")
def admin_alerts_read(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    store.mark_alerts_read(user["id"])
    return {"ok": True}


@app.get("/api/admin/recovery")
def admin_recovery(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    items = store.list_recovery_requests("open")
    for item in items:
        uid = item.get("user_id")
        item["user_status"] = (store.get_user(int(uid)) or {}).get("status", "") if uid else ""
    return {"items": items, "open": store.count_open_recovery_requests()}


@app.post("/api/admin/recovery/{request_id}/resolve")
def admin_resolve_recovery(request_id: int, user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    store.set_recovery_status(request_id, "done")
    _audit(user, "recovery.resolve", target=str(request_id), detail="处理找回密码申请")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/role")
def admin_set_role(user_id: int, payload: RoleChange, user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    role = (payload.role or "").strip().lower()
    if role not in {"user", "admin"}:
        raise HTTPException(status_code=400, detail="角色只能是 user 或 admin")
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user_id == user["id"] and role != "admin":
        raise HTTPException(status_code=400, detail="不能取消自己的管理员权限")
    if role == "user" and (target.get("role") or "user") == "admin":
        remaining = [
            row
            for row in store.list_users()
            if (row.get("role") or "user") == "admin" and row["id"] != user_id
        ]
        if not remaining:
            raise HTTPException(status_code=400, detail="至少保留一名管理员")
    store.set_user_role(user_id, role)
    _audit(
        user,
        "user.set_role",
        target=str(user_id),
        detail=f"username={target.get('username')} role={role}",
    )
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/status")
def admin_set_status(
    user_id: int, payload: StatusChange, user: dict = Depends(current_user)
) -> dict:
    """启用/停用账号。停用会注销其全部会话并停止人格对外回复，数据与账本保留。"""
    _require_admin(user)
    status = (payload.status or "").strip().lower()
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="状态只能是 active 或 disabled")
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if int(user_id) == int(user["id"]) and status != "active":
        raise HTTPException(status_code=400, detail="不能停用自己的账号")
    if status != "active" and (target.get("role") or "user") == "admin":
        remaining = [
            row
            for row in store.list_users()
            if (row.get("role") or "user") == "admin"
            and int(row["id"]) != int(user_id)
            and (row.get("status") or "active") == "active"
        ]
        if not remaining:
            raise HTTPException(status_code=400, detail="至少保留一名启用中的管理员")
    store.set_user_status(user_id, status)
    _audit(
        user,
        "user.set_status",
        target=str(user_id),
        detail=f"username={target.get('username')} status={status}",
    )
    return {"ok": True, "status": status}


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: int, payload: AdminPasswordReset, user: dict = Depends(current_user)
) -> dict:
    _require_admin(user)
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 禁止管理员重置其他管理员账号，避免单一管理员失陷后横向接管全部后台账号。
    if (target.get("role") or "user") == "admin" and int(user_id) != int(user["id"]):
        raise HTTPException(status_code=403, detail="不能重置其他管理员的密码")
    username = target.get("username") or f"user{user_id}"
    try:
        accounts.set_username_password(user_id, username, payload.password or "")
    except accounts.AccountError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    store.delete_user_sessions(user_id)
    _audit(user, "user.reset_password", target=str(user_id), detail=f"username={username}")
    return {"ok": True}


@app.get("/api/admin/platform")
def admin_get_platform(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    row = store.get_platform_config_row() or {}
    platform = load_platform_config()
    return {
        "base_url": platform.base_url,
        "model": platform.model,
        "enabled": platform.enabled,
        "per_turn_cost": platform.per_turn_cost,
        "new_user_gift": platform.new_user_gift,
        "default_credit_days": platform.default_credit_days,
        "distill_ticket_price": platform.distill_ticket_price,
        "distill_ticket_gift": platform.distill_ticket_gift,
        "has_key": bool(row.get("api_key_encrypted")),
        "api_key_masked": crypto.mask(platform.api_key) if platform.api_key else "",
        "daily_limit": platform.daily_limit,
    }


@app.put("/api/admin/platform")
def admin_set_platform(
    payload: PlatformConfigRequest, user: dict = Depends(current_user)
) -> dict:
    _require_admin(user)
    row = store.get_platform_config_row() or {}
    encrypted = row.get("api_key_encrypted") or ""
    if payload.api_key is not None and payload.api_key.strip():
        encrypted = crypto.encrypt(payload.api_key.strip())
    base_url = (payload.base_url or row.get("base_url") or "https://api.deepseek.com/v1").strip()
    model = (payload.model or row.get("model") or "deepseek-chat").strip()
    enabled = bool(payload.enabled) if payload.enabled is not None else bool(row.get("enabled"))
    per_turn_cost = (
        max(int(payload.per_turn_cost), 0) if payload.per_turn_cost is not None
        else int(row.get("per_turn_cost") or 0)
    )
    new_user_gift = (
        max(int(payload.new_user_gift), 0) if payload.new_user_gift is not None
        else int(row.get("new_user_gift") or 0)
    )
    try:
        default_credit_days = store.normalize_validity_days(
            payload.default_credit_days if payload.default_credit_days is not None
            else row.get("default_credit_days")
        )
    except store.InvalidValidityDays as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    distill_ticket_price = (
        max(int(payload.distill_ticket_price), 0) if payload.distill_ticket_price is not None
        else int(row.get("distill_ticket_price") or 0)
    )
    distill_ticket_gift = (
        max(int(payload.distill_ticket_gift), 0) if payload.distill_ticket_gift is not None
        else int(row.get("distill_ticket_gift") or 0)
    )
    store.set_platform_config(
        encrypted, base_url, model, enabled, per_turn_cost, new_user_gift,
        default_credit_days=default_credit_days,
        distill_ticket_price=distill_ticket_price,
        distill_ticket_gift=distill_ticket_gift,
    )
    _agents.clear()
    _audit(
        user, "platform.update",
        detail=(f"model={model} enabled={enabled} cost={per_turn_cost} gift={new_user_gift}"
                f" days={default_credit_days} ticket={distill_ticket_price}"
                f" ticket_gift={distill_ticket_gift}"),
    )
    return admin_get_platform(user)


@app.get("/api/admin/credits")
def admin_credits(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    return {
        "totals": store.credits_totals(),
        "coins_totals": store.coins_totals(),
        "redemption": store.redemption_stats(),
        "packages": store.list_credit_packages(active_only=False),
    }


@app.post("/api/admin/users/{user_id}/coins")
def admin_grant_coins(
    user_id: int,
    payload: CoinGrantRequest,
    user: dict = Depends(current_user),
    idempotency_key: str = Header("", alias="Idempotency-Key"),
) -> dict:
    _require_admin(user)
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    delta = int(payload.delta)
    if delta == 0:
        raise HTTPException(status_code=400, detail="变动值不能为 0")
    if abs(delta) > MAX_GRANT_DELTA:
        raise HTTPException(status_code=400, detail="单次变动值过大")
    reason = (payload.reason or "").strip() or ("管理员发放念念币" if delta > 0 else "管理员扣减念念币")
    actor = f"admin:{user.get('username') or user['id']}"
    idem = idempotency_key.strip()[:64]
    try:
        result = store.grant_coins(user_id, delta, reason=reason, actor=actor, idem=idem)
    except store.InsufficientCoins as error:
        raise HTTPException(status_code=400, detail=f"念念币不足，当前 {error.balance} 念念币") from error
    duplicate = bool(result.get("duplicate"))
    if not duplicate:
        _audit(user, "user.grant_coins", target=str(user_id), detail=f"delta={delta} reason={reason}")
    return {"ok": True, "balance": result["balance"], "duplicate": duplicate}


@app.get("/api/admin/redemption-codes")
def admin_redemption_codes(
    status: str = "", limit: int = 200, user: dict = Depends(current_user)
) -> dict:
    _require_admin(user)
    return {
        "items": store.list_redemption_codes(limit=limit, status=status),
        "stats": store.redemption_stats(),
    }


@app.post("/api/admin/redemption-codes")
def admin_create_redemption_codes(
    payload: RedemptionCodeRequest, user: dict = Depends(current_user)
) -> dict:
    _require_admin(user)
    coins = int(payload.coins)
    if coins <= 0:
        raise HTTPException(status_code=400, detail="念念币数量需大于 0")
    if coins > MAX_GRANT_DELTA or int(payload.count) > 200:
        raise HTTPException(status_code=400, detail="单次生成数量或面额过大")
    actor = f"admin:{user.get('username') or user['id']}"
    created = store.create_redemption_codes(
        int(payload.count), coins,
        batch=(payload.batch or "").strip(),
        note=(payload.note or "").strip(),
        actor=actor,
    )
    _audit(user, "redemption.create", detail=f"count={len(created)} coins={coins}")
    return {"ok": True, "items": created, "stats": store.redemption_stats()}


@app.post("/api/admin/users/{user_id}/credits")
def admin_grant_credits(
    user_id: int,
    payload: CreditGrantRequest,
    user: dict = Depends(current_user),
    idempotency_key: str = Header("", alias="Idempotency-Key"),
) -> dict:
    _require_admin(user)
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    delta = int(payload.delta)
    if delta == 0:
        raise HTTPException(status_code=400, detail="变动值不能为 0")
    if abs(delta) > MAX_GRANT_DELTA:
        raise HTTPException(status_code=400, detail="单次变动值过大")
    reason = (payload.reason or "").strip() or ("管理员发放" if delta > 0 else "管理员扣减")
    actor = f"admin:{user.get('username') or user['id']}"
    idem = idempotency_key.strip()[:64]
    try:
        result = store.grant_credits(
            user_id, delta, reason=reason, actor=actor, idem=idem,
            expires_days=payload.credit_days if delta > 0 else None,
        )
    except store.InvalidValidityDays as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except store.InsufficientCredits as error:
        raise HTTPException(status_code=400, detail=f"余额不足，当前 {error.balance} 积分") from error
    duplicate = bool(result.get("duplicate"))
    if not duplicate:
        _audit(user, "user.grant_credits", target=str(user_id),
               detail=f"delta={delta} days={payload.credit_days} reason={reason}")
    return {"ok": True, "balance": result["balance"], "duplicate": duplicate}


@app.post("/api/admin/packages")
def admin_save_package(
    payload: PackageRequest, user: dict = Depends(current_user)
) -> dict:
    _require_admin(user)
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="套餐名称不能为空")
    if int(payload.credits) <= 0:
        raise HTTPException(status_code=400, detail="积分数量需大于 0")
    if int(payload.coins) < 0:
        raise HTTPException(status_code=400, detail="念念币定价不能为负")
    if int(payload.bonus_credits) < 0 or int(payload.bonus_tickets) < 0:
        raise HTTPException(status_code=400, detail="赠送数量不能为负")
    try:
        days = store.normalize_validity_days(payload.validity_days)
        package = store.upsert_credit_package(
            payload.id, name, payload.credits, payload.price_cents,
            (payload.badge or "").strip(), payload.sort, payload.active,
            coins=payload.coins, validity_days=days,
            bonus_credits=payload.bonus_credits, bonus_tickets=payload.bonus_tickets,
        )
    except store.InvalidValidityDays as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error.args[0])) from error
    _audit(user, "package.save", target=str(package.get("id")),
           detail=f"name={name} validity_days={days}")
    return {"ok": True, "package": package}


@app.get("/api/public-config")
def public_config() -> dict:
    return {
        "site_name": "念念 Nian",
        "wechat_login_enabled": wechat_login.enabled(),
    }


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "念念 Nian",
            "short_name": "念念",
            "description": "想 ta 的时候，念念就好",
            "start_url": "/app",
            "display": "standalone",
            "background_color": "#eceef5",
            "theme_color": "#eceef5",
            "icons": [
                {
                    "src": "/static/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )
