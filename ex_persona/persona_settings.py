"""人格级设置：默认值、深合并与校验。

设置以 JSON 文本存储在 ``personas.settings`` 列中。读取时总是与默认值深度
合并，保证前端拿到的结构完整；写入时只覆盖提交的字段。
"""

import copy
import json

# 采样温度上限：超过该值后模型会退化成多语种乱码，用户完全读不懂回复。
MAX_TEMPERATURE = 1.3

DEFAULT_SETTINGS: dict = {
    "channels": {
        "wechat": {},
        "qq": {"enabled": False},
    },
    "proactive": {
        "enabled": False,
        "mode": "interval",
        "interval_hours": 3,
        "window_start": "09:00",
        "window_end": "22:00",
        "prompt": "",
        "idle_hours": 6,
        "min_gap_minutes": 60,
        "max_per_day": 6,
        "nodes": [
            {"kind": "morning", "enabled": False, "time": "08:30", "prompt": ""},
            {"kind": "goodnight", "enabled": False, "time": "22:30", "prompt": ""},
            {"kind": "anniversary", "enabled": False, "date": "01-01", "prompt": ""},
            {"kind": "care", "enabled": False, "idle_hours": 48, "prompt": ""},
        ],
    },
    "moments": {
        "enabled": False,
        "max_per_day": 2,
        "min_gap_minutes": 180,
        "prompt": "",
    },
    "advanced": {
        "merge_seconds": 4,
        "typing_indicator": True,
        "reply_image": True,
        "reply_sticker": True,
        "reply_voice": False,
        "reply_to_images": False,
        "allow_silence": False,
        "independent_session": False,
        "split_replies": True,
        "max_segments": 3,
    },
    "tuning": {
        "verbosity": 50,
        "warmth": 60,
        "humor": 30,
    },
    "voice": {
        "asr": False,
        "tts": False,
        "voice": "female-1",
        "preset": "female-1",
        "clone_voice_id": "",
        "clone_status": "none",
        "clone_contact": "",
        "collect": True,
    },
    "model": {
        "provider": "system",
        "temperature": 0.8,
        "memory_mode": "standard",
        "long_term_memory": True,
        "memory_extract_every": 6,
        "fallback_reply": "哈哈，刚在忙，晚点回你。",
    },
    "device": {
        "enabled": False,
        "type": "",
    },
    "safety": {
        "enabled": True,
        "daily_limit": 200,
        "per_minute": 12,
        "quiet_start": 0,
        "quiet_end": 7,
        "reply_delay": True,
        "delay_min": 0.8,
        "delay_max": 4.0,
        "sensitive_filter": True,
        "sensitive_action": "replace",
        "offline_alert": True,
        "crisis_support": True,
    },
}

MEMORY_MODES = {"none", "saver", "standard", "deep"}
LEGACY_MEMORY_MODES = {"full": "standard", "recent": "saver"}
PROVIDERS = {"system", "kimi", "minimax", "deepseek"}
PROACTIVE_MODES = {"interval", "smart"}
SENSITIVE_ACTIONS = {"replace", "drop"}
VOICE_PRESETS = ("female-1", "female-2", "male-1", "male-2")
VOICE_CLONE_STATUSES = {"none", "pending", "ready", "failed"}
NODE_KINDS = ("morning", "goodnight", "anniversary", "care")
NODE_LABELS = {
    "morning": "早安问候",
    "goodnight": "晚安问候",
    "anniversary": "纪念日",
    "care": "久未联系",
}

# 非 system 的厂商预置端点与默认模型。调用时统一使用用户在「模型设置」里
# 填写的 Key，因此所选厂商必须与 Key 所属平台一致，否则会返回鉴权错误。
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "minimax": {
        "base_url": "https://api.minimax.cn/v1",
        "model": "MiniMax-M3",
    },
}


def provider_model(provider: str, base_url: str, model: str) -> tuple[str, str]:
    """根据厂商预置返回 (base_url, model)；system 或未知厂商沿用用户配置。"""
    preset = PROVIDER_PRESETS.get(provider)
    if preset:
        return preset["base_url"], preset["model"]
    return base_url, model


def _merge(base: dict, patch: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load(raw: str | None) -> dict:
    """把数据库里的 JSON 文本解析为与默认值合并后的完整设置。"""
    if not raw:
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(parsed, dict):
        return copy.deepcopy(DEFAULT_SETTINGS)
    return _merge(DEFAULT_SETTINGS, parsed)


def dump(settings: dict) -> str:
    return json.dumps(settings, ensure_ascii=False)


def merge_patch(raw: str | None, patch: dict) -> dict:
    """在现有设置上应用增量 patch，返回合并后的完整设置。"""
    return _merge(load(raw), patch or {})


def validate(settings: dict) -> dict:
    """把各字段收敛到合法范围，返回修正后的设置。"""
    proactive = settings.setdefault("proactive", {})
    try:
        interval = int(proactive.get("interval_hours") or 3)
    except (TypeError, ValueError):
        interval = 3
    proactive["interval_hours"] = min(max(interval, 1), 168)
    proactive["window_start"] = _time_or(proactive.get("window_start"), "09:00")
    proactive["window_end"] = _time_or(proactive.get("window_end"), "22:00")
    if proactive.get("mode") not in PROACTIVE_MODES:
        proactive["mode"] = "interval"
    proactive["idle_hours"] = _int_range(proactive.get("idle_hours"), 6, 1, 72)
    proactive["min_gap_minutes"] = _int_range(proactive.get("min_gap_minutes"), 60, 5, 1440)
    proactive["max_per_day"] = _int_range(proactive.get("max_per_day"), 6, 1, 50)
    proactive["nodes"] = _validate_nodes(proactive.get("nodes"))

    moments = settings.setdefault("moments", {})
    moments["enabled"] = bool(moments.get("enabled", False))
    moments["max_per_day"] = _int_range(moments.get("max_per_day"), 2, 1, 10)
    moments["min_gap_minutes"] = _int_range(moments.get("min_gap_minutes"), 180, 30, 1440)
    moments["prompt"] = str(moments.get("prompt") or "")[:200]

    model = settings.setdefault("model", {})
    if model.get("provider") not in PROVIDERS:
        model["provider"] = "system"
    try:
        model["temperature"] = min(max(float(model.get("temperature", 0.8)), 0.0), MAX_TEMPERATURE)
    except (TypeError, ValueError):
        model["temperature"] = 0.8
    mode = model.get("memory_mode")
    mode = LEGACY_MEMORY_MODES.get(mode, mode)
    if mode not in MEMORY_MODES:
        mode = "standard"
    model["memory_mode"] = mode
    model["long_term_memory"] = bool(model.get("long_term_memory", True))
    try:
        every = int(model.get("memory_extract_every") or 6)
    except (TypeError, ValueError):
        every = 6
    model["memory_extract_every"] = min(max(every, 2), 50)
    fallback = model.get("fallback_reply")
    model["fallback_reply"] = str(fallback)[:200] if fallback else ""

    advanced = settings.setdefault("advanced", {})
    try:
        merge_seconds = int(advanced.get("merge_seconds") or 4)
    except (TypeError, ValueError):
        merge_seconds = 4
    advanced["merge_seconds"] = min(max(merge_seconds, 0), 60)
    advanced["split_replies"] = bool(advanced.get("split_replies", True))
    advanced["max_segments"] = _int_range(advanced.get("max_segments"), 3, 1, 4)
    advanced["reply_voice"] = bool(advanced.get("reply_voice", False))

    voice = settings.setdefault("voice", {})
    preset = voice.get("preset") or voice.get("voice") or "female-1"
    if preset not in VOICE_PRESETS:
        preset = "female-1"
    voice["voice"] = preset
    voice["preset"] = preset
    status = voice.get("clone_status")
    if status not in VOICE_CLONE_STATUSES:
        status = "none"
    voice["clone_status"] = status
    voice["clone_voice_id"] = str(voice.get("clone_voice_id") or "")[:120]
    voice["clone_contact"] = str(voice.get("clone_contact") or "")[:200]
    voice["asr"] = bool(voice.get("asr", False))
    voice["tts"] = bool(voice.get("tts", False))
    voice["collect"] = bool(voice.get("collect", True))

    tuning = settings.setdefault("tuning", {})
    tuning["verbosity"] = _int_range(tuning.get("verbosity"), 50, 0, 100)
    tuning["warmth"] = _int_range(tuning.get("warmth"), 60, 0, 100)
    tuning["humor"] = _int_range(tuning.get("humor"), 30, 0, 100)

    safety = settings.setdefault("safety", {})
    safety["enabled"] = bool(safety.get("enabled", True))
    # daily_limit / per_minute 取 0 时表示不限制。
    safety["daily_limit"] = _int_range(safety.get("daily_limit"), 200, 0, 5000)
    safety["per_minute"] = _int_range(safety.get("per_minute"), 12, 0, 600)
    safety["quiet_start"] = _int_range(safety.get("quiet_start"), 0, 0, 23)
    safety["quiet_end"] = _int_range(safety.get("quiet_end"), 7, 0, 23)
    safety["reply_delay"] = bool(safety.get("reply_delay", True))
    safety["delay_min"] = _float_range(safety.get("delay_min"), 0.8, 0.0, 60.0)
    safety["delay_max"] = _float_range(safety.get("delay_max"), 4.0, 0.0, 120.0)
    if safety["delay_max"] < safety["delay_min"]:
        safety["delay_max"] = safety["delay_min"]
    safety["sensitive_filter"] = bool(safety.get("sensitive_filter", True))
    if safety.get("sensitive_action") not in SENSITIVE_ACTIONS:
        safety["sensitive_action"] = "replace"
    safety["offline_alert"] = bool(safety.get("offline_alert", True))
    safety["crisis_support"] = bool(safety.get("crisis_support", True))
    return settings


def _validate_nodes(raw) -> list[dict]:
    """把节点开关收敛成固定四种，保留用户自定义的时间与提示词。"""
    provided: dict[str, dict] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("kind") in NODE_KINDS:
                provided[item["kind"]] = item
    nodes: list[dict] = []
    for kind in NODE_KINDS:
        item = provided.get(kind) or {}
        node = {"kind": kind, "enabled": bool(item.get("enabled", False))}
        if kind == "care":
            node["idle_hours"] = _int_range(item.get("idle_hours"), 48, 6, 720)
        elif kind == "anniversary":
            node["date"] = _month_day_or(item.get("date"), "01-01")
        else:
            default = "08:30" if kind == "morning" else "22:30"
            node["time"] = _time_or(item.get("time"), default)
        node["prompt"] = str(item.get("prompt") or "")[:200]
        nodes.append(node)
    return nodes


def _month_day_or(value, fallback: str) -> str:
    if isinstance(value, str) and len(value) == 5 and value[2] == "-":
        month, _, day = value.partition("-")
        if month.isdigit() and day.isdigit() and 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return value
    return fallback


def _time_or(value, fallback: str) -> str:
    if isinstance(value, str) and len(value) == 5 and value[2] == ":":
        hour, _, minute = value.partition(":")
        if hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60:
            return value
    return fallback


def _int_range(value, fallback: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, low), high)


def _float_range(value, fallback: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return round(min(max(number, low), high), 2)


def in_window(settings: dict, now) -> bool:
    """判断当前时间是否落在主动消息的时间窗内。"""
    start = settings["proactive"]["window_start"]
    end = settings["proactive"]["window_end"]
    current = now.strftime("%H:%M")
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def temperature(settings: dict) -> float:
    value = float(settings["model"]["temperature"])
    return min(max(value, 0.0), MAX_TEMPERATURE)


def _scale(value: int, low: str, mid: str, high: str) -> str:
    if value <= 33:
        return low
    if value >= 67:
        return high
    return mid


def tuning_hint(settings: dict) -> str:
    """把三档人格调校翻译成给模型看的提示，默认档位不产生任何文字。"""
    tuning = (settings or {}).get("tuning") or {}
    lines: list[str] = []
    verbosity = int(tuning.get("verbosity", 50))
    if verbosity <= 33:
        lines.append("回复尽量短，一两句话说完，别啰嗦。")
    elif verbosity >= 67:
        lines.append("可以多说几句，像真人聊天那样有铺陈和细节。")
    warmth = int(tuning.get("warmth", 60))
    if warmth <= 33:
        lines.append("语气克制理性，别太黏人，保持淡淡的分寸感。")
    elif warmth >= 67:
        lines.append("语气亲密黏人，多用昵称与关心，让对方感到被在乎。")
    humor = int(tuning.get("humor", 30))
    if humor <= 20:
        lines.append("少开玩笑，保持认真和真诚。")
    elif humor >= 60:
        lines.append("可以适度打趣、玩梗，让对话轻松一点。")
    if not lines:
        return ""
    return "【人格调校】\n" + "\n".join(lines)


def enabled_nodes(settings: dict) -> list[dict]:
    nodes = ((settings or {}).get("proactive") or {}).get("nodes") or []
    return [node for node in nodes if isinstance(node, dict) and node.get("enabled")]


def memory_limit(settings: dict) -> int:
    """把记忆档位换算成保留的历史轮数；省积分只记最近几条。"""
    mode = settings["model"]["memory_mode"]
    return {"saver": 6, "standard": 16, "deep": 40, "none": 0}.get(mode, 16)
