"""解析回复中的媒体指令，并从人格素材库挑选要发送的图片。

模型在开启 ``advanced.reply_sticker`` / ``advanced.reply_image`` 后，可以在回复里
用 ``[[STICKER]]``、``[[STICKER:2]]``、``[[STICKER:关键词]]`` 这类标记表达
"想发一张图"。这里负责把标记从正文中剥离，并在素材库中选出对应文件。
"""

import random
import re

_MARKER = re.compile(r"\[\[\s*(STICKER|IMAGE)\s*(?:[:：]\s*([^\]\n]*?))?\s*\]\]", re.IGNORECASE)


def enabled_kinds(settings: dict) -> set[str]:
    advanced = (settings or {}).get("advanced", {})
    kinds: set[str] = set()
    if advanced.get("reply_sticker"):
        kinds.add("STICKER")
    if advanced.get("reply_image"):
        kinds.add("IMAGE")
    return kinds


def parse_reply(reply: str, allowed: set[str]) -> tuple[str, list[dict]]:
    """剥离媒体标记，返回 (正文, 指令列表)；未启用的类型原样保留。"""
    directives: list[dict] = []

    def _replace(match: re.Match) -> str:
        kind = match.group(1).upper()
        if kind not in allowed:
            return match.group(0)
        directives.append({"kind": kind, "keyword": (match.group(2) or "").strip()})
        return ""

    cleaned = _MARKER.sub(_replace, reply or "")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, directives


def choose_media(stickers: list[dict], directive: dict, rng: random.Random | None = None) -> dict | None:
    """按编号或名称关键词挑选素材；无法精确命中时回退到随机一张。"""
    if not stickers:
        return None
    keyword = (directive or {}).get("keyword", "").strip()
    if keyword:
        if keyword.isdigit():
            index = int(keyword) - 1
            if 0 <= index < len(stickers):
                return stickers[index]
        lowered = keyword.lower()
        for sticker in stickers:
            name = (sticker.get("name") or "").lower()
            if lowered and lowered in name:
                return sticker
    return (rng or random).choice(stickers)


def build_hint(stickers: list[dict], settings: dict) -> str:
    """生成系统提示词片段，告诉模型可以发表情以及可用编号。"""
    kinds = enabled_kinds(settings)
    if not kinds or not stickers:
        return ""
    lines = ["【表情包】", "必要时可以发表情包让对话更自然，但别每条都发，也别解释。"]
    if "STICKER" in kinds:
        lines.append(f"可用表情编号：{'、'.join(str(i + 1) for i in range(len(stickers)))}")
        lines.append("想发表情时，在回复最后单独写 [[STICKER]]，或指定编号 [[STICKER:2]]。")
    if "IMAGE" in kinds:
        lines.append("想发图片时，可写 [[IMAGE]] 或 [[IMAGE:编号]]。")
    return "\n".join(lines)
