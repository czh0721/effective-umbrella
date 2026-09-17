"""人格档案文件的读写：把结构化的卡片与风格写成标准档案目录。

标签页字段约定（``persona_card.json`` 内）：

- ``identity``：姓名 / 生日 / 年龄 / 籍贯 / 学历
- ``user``：与用户的关系 / 认识时间
- ``soul``：说话风格 / 口头禅
- ``memories``：共同回忆
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .distill import render_skill, write_text_atomic

IDENTITY_FIELDS = ["name", "gender", "birthday", "age", "hometown", "education"]
USER_FIELDS = ["relationship", "met_time"]
MEMORY_FIELDS = ["title", "detail", "time"]


def empty_style() -> dict:
    return {
        "message_count": 0,
        "avg_length": 0,
        "median_length": 0,
        "max_length": 0,
        "short_message_ratio": 0,
        "question_ratio": 0,
        "exclaim_ratio": 0,
        "ellipsis_ratio": 0,
        "tilde_ratio": 0,
        "no_end_punctuation_ratio": 0,
        "top_words": [],
        "top_emoji": [],
        "top_bracket_emoticons": [],
        "top_openers": [],
        "top_closers": [],
    }


def blank_card(name: str) -> dict:
    return {
        "summary": "",
        "personality": [],
        "speaking_style": [],
        "emotional_patterns": [],
        "values_and_attitudes": [],
        "relationship_with_me": [],
        "favorite_phrases": [],
        "topics": [],
        "boundaries": [],
        "identity": {"name": name, "gender": "", "birthday": "", "age": "",
                      "hometown": "", "education": ""},
        "user": {"relationship": "", "met_time": ""},
        "soul": {"speaking_style": [], "catchphrases": []},
        "memories": [],
    }


def normalize_card(card: dict, name: str = "") -> dict:
    """补齐可能缺失的标签页字段，兼容旧档案。"""
    base = blank_card(name)
    if not isinstance(card, dict):
        return base
    for key, value in card.items():
        if key in ("identity", "user", "soul") and isinstance(value, dict):
            base[key].update({k: v for k, v in value.items() if k in base[key]})
        elif key == "memories" and isinstance(value, list):
            base["memories"] = [item for item in value if isinstance(item, dict)]
        elif key in base:
            base[key] = value
        else:
            base[key] = value
    return base


def write_files(directory: Path, name: str, card: dict, style: dict, meta: dict | None = None) -> dict:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    card = normalize_card(card, name)
    style = style or empty_style()

    write_text_atomic(
        directory / "persona_card.json", json.dumps(card, ensure_ascii=False, indent=2)
    )
    write_text_atomic(
        directory / "style.json", json.dumps(style, ensure_ascii=False, indent=2)
    )
    skill = render_skill(name, card, style)
    write_text_atomic(directory / "SKILL.md", skill)

    profile = {
        "name": name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "used_llm": bool((meta or {}).get("used_llm", False)),
        "message_count": style.get("message_count", 0),
        "pair_count": (meta or {}).get("pair_count", 0),
        "senders": (meta or {}).get("senders", {}),
        "sources": (meta or {}).get("sources", []),
        "preset": (meta or {}).get("preset", ""),
    }
    write_text_atomic(
        directory / "profile.json", json.dumps(profile, ensure_ascii=False, indent=2)
    )
    return profile


def read_card(directory: Path, name: str = "") -> dict:
    path = Path(directory) / "persona_card.json"
    if not path.exists():
        return normalize_card({}, name)
    try:
        return normalize_card(json.loads(path.read_text(encoding="utf-8")), name)
    except (ValueError, OSError):
        return normalize_card({}, name)
