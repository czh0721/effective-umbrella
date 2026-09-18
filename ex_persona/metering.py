"""模型 token 计量：把每次模型调用的真实用量落库，供成本核算。

调用方用 ``bind(...)`` 声明当前操作用户 / 人格 / 用途；``llm`` 在每次请求返回后
调用 ``record``。没有绑定上下文时仍会记录 token（归属记为 0 / unknown），保证
成本不丢失。
"""

import contextvars
import logging

log = logging.getLogger("ex_persona.metering")

_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar("nian_metering", default=None)


class _Binding:
    def __init__(self, fields: dict) -> None:
        self._fields = fields
        self._token = None

    def __enter__(self):
        self._token = _CTX.set(dict(self._fields))
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _CTX.reset(self._token)
        return False


def bind(
    user_id: int = 0,
    persona_id: int = 0,
    purpose: str = "unknown",
    contact: str = "",
) -> _Binding:
    """声明一段操作期间的计量归属，用于上下文管理器。"""
    return _Binding(
        {
            "user_id": int(user_id or 0),
            "persona_id": int(persona_id or 0),
            "purpose": purpose or "unknown",
            "contact": contact or "",
        }
    )


def current() -> dict:
    return _CTX.get() or {}


def _get(obj, key: str, default: int = 0) -> int:
    if obj is None:
        return default
    value = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def read_usage(usage) -> dict:
    """从 OpenAI/DeepSeek 的 usage 对象或字典里读出 token 明细。"""
    prompt = _get(usage, "prompt_tokens")
    completion = _get(usage, "completion_tokens")
    total = _get(usage, "total_tokens") or (prompt + completion)
    cached = _get(usage, "prompt_cache_hit_tokens")
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(
        usage, "prompt_tokens_details", None
    )
    if not cached and details is not None:
        cached = _get(details, "cached_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": total,
    }


def record(config, usage, purpose: str = "") -> None:
    """记录一次模型调用的 token 用量；计量失败不影响业务。"""
    data = read_usage(usage)
    if not (data["prompt_tokens"] or data["completion_tokens"] or data["total_tokens"]):
        return
    ctx = current()
    try:
        from . import store

        store.record_token_usage(
            user_id=ctx.get("user_id", 0),
            persona_id=ctx.get("persona_id", 0),
            contact=ctx.get("contact", ""),
            purpose=purpose or ctx.get("purpose", "unknown"),
            model=str(getattr(config, "model", "") or ""),
            platform=1 if getattr(config, "platform", False) else 0,
            prompt_tokens=data["prompt_tokens"],
            completion_tokens=data["completion_tokens"],
            cached_tokens=data["cached_tokens"],
            total_tokens=data["total_tokens"],
        )
    except Exception:  # noqa: BLE001 - 计量失败不能影响正常回复
        log.warning("token usage record failed", exc_info=True)
