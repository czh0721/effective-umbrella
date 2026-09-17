"""真人感回复：把一条长回复拆成几条短消息，并给出自然的打字节奏。

微信里真人很少一次发一大段，而是分成好几条短消息。这里把模型输出切成
至多 ``max_segments`` 段：优先按空行、换行、句末标点切分，再按长度把碎片
合并成均衡的几段，避免出现只有一个字的碎消息。
"""

import re

# 句末标点（含中英文与省略号、波浪号）作为次级切分点。
_SENTENCE_END = re.compile(r"(?<=[。！？!?…~～])\s*")
# 单段最长字符数，超过就不再切分，避免把一句话拆得莫名其妙。
MAX_SEGMENT_CHARS = 160


def _lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        for line in block.splitlines():
            line = line.strip()
            if line:
                lines.append(line)
    return lines


def _split_units(text: str) -> list[str]:
    units: list[str] = []
    for line in _lines(text):
        pieces = [piece.strip() for piece in _SENTENCE_END.split(line) if piece.strip()]
        units.extend(pieces or [line])
    return units


def _explicit_bubbles(text: str) -> list[str]:
    """按模型主动写的换行拆成「有意连发」的气泡，不再按长度合并。"""
    return _lines(text)


def split_reply(text: str, max_segments: int = 3) -> list[str]:
    """把回复切成至多 ``max_segments`` 段；无需切分时返回单元素列表。

    模型主动用换行分隔时，即使每条都很短也按多条消息原样发出（真人连发的典型形态）；
    单行且不长时保持一条，避免把正常短句拆碎。超长单行才按句子切分。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    max_segments = max(int(max_segments or 1), 1)
    if max_segments == 1:
        return [cleaned]

    if "\n" in cleaned:
        bubbles = _explicit_bubbles(cleaned)
        if len(bubbles) > 1:
            if len(bubbles) <= max_segments:
                return bubbles
            # 条数超出上限：保留前几条，其余并成最后一条，避免丢内容。
            return bubbles[: max_segments - 1] + ["".join(bubbles[max_segments - 1 :])]

    units = _split_units(cleaned)
    if len(units) <= 1 or len(cleaned) <= MAX_SEGMENT_CHARS:
        return [cleaned]

    # 均分到 max_segments 组：按单位数平分，尽量让每组长度接近。
    target = min(max_segments, len(units))
    total = sum(len(unit) for unit in units)
    per_group = total / target
    groups: list[str] = []
    current: list[str] = []
    current_len = 0
    remaining_groups = target
    for index, unit in enumerate(units):
        remaining_units = len(units) - index
        current.append(unit)
        current_len += len(unit)
        must_flush = remaining_units == remaining_groups - 1 and remaining_groups > 1
        near_limit = current_len >= per_group and remaining_groups > 1
        if must_flush or near_limit:
            groups.append("".join(current).strip())
            current = []
            current_len = 0
            remaining_groups -= 1
    if current:
        groups.append("".join(current).strip())
    groups = [group for group in groups if group]
    return groups or [cleaned]


def segment_delay(segment: str, *, minimum: float = 0.8, maximum: float = 6.0) -> float:
    """按消息长度估算排队间隔，让多条消息的到达节奏更像真人打字。"""
    length = len(segment or "")
    seconds = minimum + length * 0.03
    return round(min(max(seconds, minimum), maximum), 2)
