"""主动消息：按人格设置的时间窗与频率，定时用 weclaw 主动发消息。

``interval`` 模式按固定间隔发送；``smart`` 模式会结合最近对话、沉默时长、
时间段与当日发送次数，让模型自行判断此刻是否适合开口，不适合则跳过。
"""

import threading
from datetime import datetime, timezone

from . import clock, persona_settings, store

DEFAULT_PROMPT = (
    "现在没有人给你发消息。请你以角色本人的身份，主动给对方发一条自然、简短的微信，"
    "可以问候、分享近况，或提起你们的共同回忆。像平时聊天一样，只输出消息内容本身。"
)

SKIP_MARKER = "[[SKIP]]"

# 一次主动消息尝试（调用大模型或发送）失败或被打断后，多久内不再重试，
# 避免模型返回 SKIP / 桥接暂时不可用时每 60 秒重复消耗一次大模型。
ATTEMPT_COOLDOWN_SECONDS = 600

# 定时节点（早安/晚安）落在时间窗之外时，允许的迟到上限，避免服务长时间
# 停机后深夜补发「早安」。节点自带明确时间，因此不受时间窗硬性拦截。
NODE_GRACE_MINUTES = 120

# 节点化主动消息的默认提示词；用户在节点里填写提示词时优先使用用户版本。
NODE_PROMPTS = {
    "morning": (
        "现在是早上。请你以角色本人的身份，给对方发一条自然、简短的早安消息，"
        "可以带一点关心或今天的小安排，只有一两句话，只输出消息内容本身。"
    ),
    "goodnight": (
        "现在是晚上，对方准备休息了。请你以角色本人的身份，给对方发一条自然、简短的晚安消息，"
        "语气温柔，只有一两句话，只输出消息内容本身。"
    ),
    "anniversary": (
        "今天是你们的纪念日（{date}）。请你以角色本人的身份，发一条自然、简短的纪念消息，"
        "提起你们之间的共同回忆，只有一两句话，只输出消息内容本身。"
    ),
    "care": (
        "你们已经 {idle} 没有聊天了。请你以角色本人的身份，主动发一条自然、简短的消息，"
        "关心一下对方最近怎么样，只有一两句话，只输出消息内容本身。"
    ),
}

SMART_PROMPT = (
    "现在是{period}（{clock}），距离你们上一次聊天已经过去 {idle}。\n"
    "{recent}"
    "今天你已经主动找过对方 {sent_today} 次。\n\n"
    "请你判断此刻是否适合主动给对方发一条微信：\n"
    "如果适合，就直接输出要发送的消息内容本身，像平时聊天一样，不要解释；\n"
    "如果不适合（刚聊完、时间太晚、没什么新话题、今天已经发得够多等），"
    "只输出 {skip}，不要输出其他任何内容。"
)


def _period(hour: int) -> str:
    """兼容旧调用：时段判断统一走 clock。"""
    return clock.period(hour)


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


def _humanize(seconds: float) -> str:
    minutes = int(max(seconds, 0) // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


def _clock_minutes(value) -> int | None:
    """把 ``"HH:MM"`` 解析成当天分钟数；非法值返回 None。"""
    text = str(value or "")
    hours, _, minutes = text.partition(":")
    try:
        hour = int(hours)
        minute = int(minutes)
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


class ProactiveScheduler:
    """后台轮询，为开启主动消息的人格发送问候。"""

    def __init__(self, agent_factory, send_func, check_seconds: int = 60) -> None:
        self._agent_factory = agent_factory
        self._send = send_func
        self._check_seconds = max(int(check_seconds), 10)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sent: dict[int, float] = {}
        self._last_attempt: dict[int, float] = {}

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
        for persona in store.list_proactive_personas():
            settings = persona_settings.load(persona.get("settings"))
            proactive = settings["proactive"]
            if not proactive.get("enabled"):
                continue
            contact = persona.get("last_contact")
            if not contact:
                continue

            if self._daily_capped(persona, proactive, now):
                continue

            # 上一次尝试（含失败/SKIP）刚发生不久，先冷却，避免高频重复调用大模型。
            last_attempt = self._last_attempt.get(persona["id"], 0.0)
            if last_attempt and now_ts - last_attempt < ATTEMPT_COOLDOWN_SECONDS:
                continue

            in_window = persona_settings.in_window(settings, now)

            # 节点化消息优先：命中的节点直接发一条，并跳过本轮的其他触发。
            # 节点自带明确时间（早安/晚安等），不受时间窗硬性拦截，但窗外的
            # 迟到有上限，避免停机后在半夜补发。
            node_prompt, node_kind = self._due_node(
                persona, settings, proactive, contact, now, now_ts, in_window
            )
            if node_kind:
                self._deliver(persona, contact, node_prompt, node_kind, now_ts)
                continue

            # 非节点消息（间隔/智能）必须落在用户设置的时间窗内。
            if not in_window:
                continue

            mode = proactive.get("mode") or "interval"
            if mode == "smart":
                if self._smart_cooling_down(persona, proactive, now, now_ts):
                    continue
                idle_seconds = self._idle_seconds(persona, contact, now_ts)
                idle_hours = float(proactive.get("idle_hours") or 6) * 3600
                if idle_seconds is None or idle_seconds < idle_hours:
                    continue
                prompt = self._smart_prompt(persona, contact, proactive, now, idle_seconds)
            else:
                interval = float(proactive.get("interval_hours") or 3) * 3600
                last = max(
                    _epoch(store.last_proactive_at(persona["id"])),
                    self._last_sent.get(persona["id"], 0.0),
                )
                if last and now_ts - last < interval:
                    continue
                prompt = (proactive.get("prompt") or "").strip() or DEFAULT_PROMPT
            self._deliver(persona, contact, prompt, "", now_ts)

    def _deliver(self, persona: dict, contact: str, prompt: str, kind: str, now_ts: float) -> bool:
        """生成并发送一条主动消息；成功时写入会话与主动消息日志。"""
        try:
            agent = self._agent_factory(persona["user_id"], persona)
            text = agent.reply(prompt)
        except Exception:  # noqa: BLE001
            self._last_attempt[persona["id"]] = now_ts
            return False
        text = (text or "").strip()
        if not text or SKIP_MARKER in text or text.upper() == "SKIP":
            self._last_attempt[persona["id"]] = now_ts
            return False
        if not self._send(persona["user_id"], contact, text):
            self._last_attempt[persona["id"]] = now_ts
            return False
        self._last_sent[persona["id"]] = now_ts
        store.add_turn(persona["user_id"], persona["id"], "assistant", text, contact=contact)
        store.add_proactive_log(persona["user_id"], persona["id"], contact, text, kind=kind)
        return True

    def _morning_start(self, now: datetime) -> str:
        return datetime.fromtimestamp(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), timezone.utc
        ).isoformat()

    def _daily_capped(self, persona: dict, proactive: dict, now: datetime) -> bool:
        cap = int(proactive.get("max_per_day") or 6)
        if cap <= 0:
            return False
        return store.count_proactive_since(persona["id"], self._morning_start(now)) >= cap

    def _due_node(
        self,
        persona: dict,
        settings: dict,
        proactive: dict,
        contact: str,
        now: datetime,
        now_ts: float,
        in_window: bool,
    ) -> tuple[str, str]:
        """返回当前应当触发的 (提示词, 节点类型)；没有就绪节点时返回空。

        定时节点（早安/晚安）带明确时间，即使落在时间窗之外也会触发，但窗外的
        迟到受 ``NODE_GRACE_MINUTES`` 限制；其余节点（纪念日/关心）没有时间点，
        仍必须落在时间窗内，避免半夜打扰。
        """
        today = now.strftime("%m-%d")
        now_minutes = now.hour * 60 + now.minute
        since = self._morning_start(now)
        for node in persona_settings.enabled_nodes(settings):
            kind = node.get("kind")
            if store.count_proactive_since(persona["id"], since, kind):
                continue
            if kind in ("morning", "goodnight"):
                node_minutes = _clock_minutes(node.get("time") or "23:59")
                if node_minutes is None or now_minutes < node_minutes:
                    continue
                if not in_window and now_minutes - node_minutes > NODE_GRACE_MINUTES:
                    continue
            elif kind == "anniversary":
                if today != str(node.get("date") or ""):
                    continue
                if not in_window:
                    continue
            elif kind == "care":
                idle_seconds = self._idle_seconds(persona, contact, now_ts)
                need = float(node.get("idle_hours") or 48) * 3600
                if idle_seconds is None or idle_seconds < need:
                    continue
                if not in_window:
                    continue
            else:
                continue
            default = NODE_PROMPTS.get(kind, DEFAULT_PROMPT)
            if kind == "anniversary":
                default = default.format(date=node.get("date") or today)
            elif kind == "care":
                idle_seconds = self._idle_seconds(persona, contact, now_ts) or 0
                default = default.format(idle=_humanize(idle_seconds))
            prompt = (node.get("prompt") or "").strip() or default
            return prompt, str(kind)
        return "", ""

    def _smart_cooling_down(
        self, persona: dict, proactive: dict, now: datetime, now_ts: float
    ) -> bool:
        gap = float(proactive.get("min_gap_minutes") or 60) * 60
        last = max(
            _epoch(store.last_proactive_at(persona["id"])),
            self._last_sent.get(persona["id"], 0.0),
        )
        if last and now_ts - last < gap:
            return True
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = datetime.fromtimestamp(midnight.timestamp(), timezone.utc).isoformat()
        sent_today = store.count_proactive_since(persona["id"], since)
        return sent_today >= int(proactive.get("max_per_day") or 6)

    def _idle_seconds(self, persona: dict, contact: str, now_ts: float) -> float | None:
        last = _epoch(store.last_turn_at(persona["user_id"], persona["id"], contact, "user"))
        if not last:
            return None
        return now_ts - last

    def _smart_prompt(
        self, persona: dict, contact: str, proactive: dict, now: datetime, idle_seconds: float
    ) -> str:
        turns = store.list_turns(persona["user_id"], persona["id"], limit=8, contact=contact)
        if turns:
            lines = [
                f"{'对方' if turn['role'] == 'user' else '你'}：{turn['content'][:120]}"
                for turn in turns
            ]
            recent = "最近对话：\n" + "\n".join(lines) + "\n"
        else:
            recent = "你们还没有聊过。\n"
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = datetime.fromtimestamp(midnight.timestamp(), timezone.utc).isoformat()
        sent_today = store.count_proactive_since(persona["id"], since)
        return SMART_PROMPT.format(
            period=_period(now.hour),
            clock=now.strftime("%H:%M"),
            idle=_humanize(idle_seconds),
            recent=recent,
            sent_today=sent_today,
            skip=SKIP_MARKER,
        )
