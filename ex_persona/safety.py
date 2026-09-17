"""防封安全策略：敏感词过滤、拟人回复延迟、静默时段与发送配额。

这一层是消息真正发往微信前的最后一道把关：内容命中敏感词时替换或丢弃，
发送频率超过人格配额时拒绝，凌晨时段不主动打扰。所有阈值来自人格设置
``safety`` 分组，运营者也可以通过环境变量覆盖敏感词库路径。
"""

import os
import random
import threading
from pathlib import Path

# 敏感词库默认路径；可用 PERSONA_SENSITIVE_WORDS 指向自定义文件。
DEFAULT_WORDS_FILE = Path(__file__).resolve().parent / "data" / "sensitive_words.txt"
WORDS_ENV = "PERSONA_SENSITIVE_WORDS"
ACTION_REPLACE = "replace"
ACTION_DROP = "drop"
REPLACEMENT = "***"

_cache: dict = {"path": None, "mtime": None, "words": ()}
_cache_lock = threading.Lock()


def words_path() -> Path:
    override = (os.getenv(WORDS_ENV) or "").strip()
    return Path(override).expanduser() if override else DEFAULT_WORDS_FILE


def _load_file(path: Path) -> tuple[str, ...]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    words = [line.strip() for line in raw.splitlines()]
    words = [word for word in words if word and not word.startswith("#")]
    # 长词优先，避免短词先命中导致替换不完整。
    return tuple(sorted(set(words), key=len, reverse=True))


def load_words() -> tuple[str, ...]:
    """读取敏感词库；按文件修改时间缓存，运维改词库无需重启。"""
    path = words_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ()
    with _cache_lock:
        if _cache["path"] == str(path) and _cache["mtime"] == mtime:
            return _cache["words"]
        words = _load_file(path)
        _cache.update(path=str(path), mtime=mtime, words=words)
        return words


def scrub(text: str, *, action: str = ACTION_REPLACE) -> tuple[str, str]:
    """按策略过滤文本，返回 (处理后文本, 命中的词)。

    ``drop`` 把整条消息置空（宁可少发也不冒险）；``replace`` 只替换命中词。
    """
    if not text:
        return text, ""
    hits = [word for word in load_words() if word in text]
    if not hits:
        return text, ""
    if action == ACTION_DROP:
        return "", hits[0]
    result = text
    for word in hits:
        result = result.replace(word, REPLACEMENT)
    return result, hits[0]


def reply_delay(
    text: str,
    *,
    minimum: float,
    maximum: float,
    rng: random.Random | None = None,
) -> float:
    """按回复长度给出拟人化延迟秒数：短回复延迟短，长回复延迟略长。

    默认在 [minimum, maximum] 内取值，长文本偏向区间上沿，并叠加少量随机
    抖动，让节奏更像真人打字而不是固定间隔。
    """
    low = max(float(minimum), 0.0)
    high = max(float(maximum), low)
    if high <= 0:
        return 0.0
    length = len(text or "")
    ratio = min(length / 60.0, 1.0)
    base = low + (high - low) * ratio
    jitter = (high - low) * 0.15
    lower = max(base - jitter, low)
    upper = min(base + jitter, high)
    generator = rng or random
    return round(generator.uniform(lower, upper), 2)


# 情绪危机识别：命中后跳过常规回复，改为给用户确定性的求助资源，
# 避免大模型在自伤/极端情绪语境下继续角色扮演。
CRISIS_PHRASES = (
    "自杀", "自残", "自尽", "轻生", "割腕", "跳楼", "跳河", "上吊", "服毒",
    "烧炭", "遗书", "遗言", "不想活了", "不想活", "不活了", "活不下去",
    "活着没意思", "活着没意义", "活着好累", "结束生命", "结束自己",
    "了结自己", "死了算了", "不如死了", "想死", "一了百了",
    "离开这个世界", "不想继续了",
)
# 这些说法里的关键字是亲昵/夸张表达，需要排除，避免误判。
CRISIS_BENIGN = ("想死你", "想死我", "想死个人", "想死你们")
CRISIS_RESOURCES = (
    "全国心理援助热线 12356（24 小时）",
    "北京心理危机研究与干预中心 010-82951332（24 小时）",
    "希望 24 热线 400-161-9995",
    "紧急情况请立即拨打 110 或 120",
)
CRISIS_TEMPLATE = (
    "{greeting}我很在意你刚刚说的话。你现在的感受很重要，也值得被认真对待。\n"
    "在你撑过这段最难的时候，请让专业的人陪着你：\n"
    "{resources}\n"
    "如果身边有人，也可以先告诉 ta。你不是一个人。"
)


def detect_crisis(text: str) -> str:
    """返回命中的危机短语；没有命中则返回空串。"""
    if not text:
        return ""
    sample = text
    for benign in CRISIS_BENIGN:
        sample = sample.replace(benign, "")
    for phrase in CRISIS_PHRASES:
        if phrase in sample:
            return phrase
    return ""


def crisis_reply(name: str = "") -> str:
    """生成危机干预回复，附带可拨打的求助资源。"""
    greeting = f"{name}在这里。" if name else ""
    resources = "\n".join(f"· {item}" for item in CRISIS_RESOURCES)
    return CRISIS_TEMPLATE.format(greeting=greeting, resources=resources)


def in_quiet_hours(settings: dict, now) -> bool:
    """判断当前时刻是否落在「不主动打扰」的静默时段。"""
    safety = settings.get("safety") or {}
    try:
        start = int(safety.get("quiet_start", 0))
        end = int(safety.get("quiet_end", 7))
    except (TypeError, ValueError):
        start, end = 0, 7
    start %= 24
    end %= 24
    hour = int(now.hour)
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end
