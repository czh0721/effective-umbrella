"""分身朋友圈：动态生成与后台自动发布。

生成复用聊天的人格 Agent（``PersonaAgent.compose_moment``），但使用朋友圈专用的
写作规则，不注入聊天历史，让动态读起来像分身自己在记录生活。
"""

import threading
from datetime import datetime, timezone

from . import clock, media_reply, persona_settings, store

# 朋友圈写作规则：与 CHAT_STYLE_RULES 分离，避免把一对一聊天的口吻带进动态。
MOMENT_STYLE_RULES = """【朋友圈写作方式】
- 你在发自己的朋友圈，不是跟某个人聊天，正文里不要出现「你」「回复」「消息」这类对话措辞。
- 用第一人称，像随手记下此刻的生活：在做什么、想到什么、什么心情。
- 一到三句，口语化，可以带一点情绪、标点或表情，但不要写小作文、不要分点、不要标题。
- 不要提到 AI、模型、提示词，也不要复述这段说明。
- 想配一张图时，在正文最后单独写一个 [[IMAGE]]；不需要配图就不要写。"""

MOMENT_PROMPT = (
    "现在是{period}（{clock}）。请你以角色本人的身份发一条朋友圈，分享此刻的生活片段"
    "或心情，一到三句，只输出动态正文本身。"
)


def _period(hour: int) -> str:
    """兼容旧调用：时段判断统一走 clock。"""
    return clock.period(hour)


def build_prompt(settings: dict, memories: list[dict], now: datetime | None = None) -> str:
    """拼装生成动态的用户指令：时间 + 用户自定义方向 + 可用作素材的近期记忆。"""
    now = now or clock.now_local()
    parts = [MOMENT_PROMPT.format(period=clock.period(now.hour), clock=now.strftime("%H:%M"))]
    custom = str(((settings or {}).get("moments") or {}).get("prompt") or "").strip()
    if custom:
        parts.append(f"【希望的内容方向】\n{custom}")
    lines = [f"- {str(item.get('content') or '')[:120]}" for item in (memories or [])[:8]]
    lines = [line for line in lines if line != "- "]
    if lines:
        parts.append("【你记得的事，可作素材】\n" + "\n".join(lines))
    return "\n\n".join(parts)


def compose(
    agent,
    settings: dict,
    memories: list[dict],
    stickers: list[dict] | None = None,
    now: datetime | None = None,
) -> tuple[str, int]:
    """生成一条动态，返回 (正文, 配图 sticker_id)。

    正文为空表示生成失败（模型未就绪、输出被过滤等），调用方应据此跳过发布。
    """
    text = agent.compose_moment(build_prompt(settings, memories, now))
    text = (text or "").strip()
    if not text:
        return "", 0

    allowed = media_reply.enabled_kinds(settings)
    # 无论是否允许配图，都先剥掉 [[IMAGE]] 标记，避免内部指令出现在动态正文里。
    cleaned, directives = media_reply.parse_reply(text, {"IMAGE"})
    cleaned = cleaned.strip()
    if not cleaned:
        return "", 0

    sticker_id = 0
    directives = [item for item in directives if item.get("kind") == "IMAGE"]
    if directives and stickers and "IMAGE" in allowed:
        chosen = media_reply.choose_media(stickers, directives[0])
        if chosen:
            sticker_id = int(chosen.get("id") or 0)
    return cleaned, sticker_id


# 一次自动发布失败（扣费后生成失败、模型未就绪等）后，多久内不再重试该人格，
# 避免模型持续异常时每轮都消耗一次调用并反复退款。
ATTEMPT_COOLDOWN_SECONDS = 600


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _epoch(value: str) -> float:
    parsed = _parse_iso(value)
    return parsed.timestamp() if parsed else 0.0


def _day_start_utc(now: datetime) -> str:
    """本地时区当天零点对应的 UTC ISO 时间，用于统计「今天发了几条」。"""
    local = now if now.tzinfo is not None else now.replace(tzinfo=clock.local_tz())
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat()


class MomentScheduler:
    """后台轮询，为开启朋友圈的人格自动生成并发布动态。"""

    def __init__(
        self,
        agent_factory,
        charge_func,
        refund_func,
        notify_func,
        check_seconds: int = 300,
    ) -> None:
        self._agent_factory = agent_factory
        self._charge = charge_func
        self._refund = refund_func
        self._notify = notify_func
        self._check_seconds = max(int(check_seconds), 10)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_publish: dict[int, float] = {}
        self._last_attempt: dict[int, float] = {}
        self._notified_day: dict[int, str] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._check_seconds):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                continue

    def tick(self, now: datetime | None = None) -> None:
        now = now or clock.now_local()
        now_ts = now.timestamp()
        day = now.strftime("%Y-%m-%d")
        since = _day_start_utc(now)
        for persona in store.list_proactive_personas():
            settings = persona_settings.load(persona.get("settings"))
            config = settings["moments"]
            if not config.get("enabled"):
                continue
            persona_id = persona["id"]
            if store.count_moments_since(persona_id, since) >= int(
                config.get("max_per_day") or 2
            ):
                continue
            if not persona_settings.in_window(settings, now):
                continue

            last_attempt = self._last_attempt.get(persona_id, 0.0)
            if last_attempt and now_ts - last_attempt < ATTEMPT_COOLDOWN_SECONDS:
                continue
            gap = float(config.get("min_gap_minutes") or 180) * 60
            last = max(_epoch(store.last_moment_at(persona_id)), self._last_publish.get(persona_id, 0.0))
            if last and now_ts - last < gap:
                continue

            self._publish(persona, settings, now, now_ts, day)

    def _publish(
        self, persona: dict, settings: dict, now: datetime, now_ts: float, day: str
    ) -> None:
        user_id = persona["user_id"]
        persona_id = persona["id"]
        agent = self._agent_factory(user_id, persona)
        if not getattr(agent.config, "ready", False):
            self._last_attempt[persona_id] = now_ts
            return
        try:
            charged = int(self._charge(user_id, persona_id) or 0)
        except store.InsufficientCredits:
            self._last_attempt[persona_id] = now_ts
            if self._notified_day.get(persona_id) != day:
                self._notified_day[persona_id] = day
                self._notify(
                    user_id,
                    "credits_moment",
                    "积分已用完，朋友圈已暂停发布；去设置页兑换念念币或购买积分套餐可继续。",
                )
            return
        except Exception:  # noqa: BLE001 - 扣费异常不应中断调度循环
            self._last_attempt[persona_id] = now_ts
            return

        try:
            stickers = store.list_stickers(user_id, persona_id)
            memories = store.list_memories(user_id, persona_id, limit=12)
            text, sticker_id = compose(agent, settings, memories, stickers, now)
        except Exception:  # noqa: BLE001
            text, sticker_id = "", 0
        if not text:
            self._refund(user_id, charged, persona_id)
            self._last_attempt[persona_id] = now_ts
            return
        try:
            store.add_moment(
                user_id, persona_id, text, sticker_id=sticker_id, source="auto"
            )
        except Exception:  # noqa: BLE001
            self._refund(user_id, charged, persona_id)
            self._last_attempt[persona_id] = now_ts
            return
        self._last_publish[persona_id] = now_ts
