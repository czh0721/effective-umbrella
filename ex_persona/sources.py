"""多数据源解析器。

每个解析器返回一组「原始条目」(dict)，交给 ingest.normalize() 归一化。
条目可带两个扩展字段：
- subject=True  : 该内容是「要复刻的人」本人产出的（社交动态、照片），直接算作目标消息
- kind         : text | post | photo，用于后续区分处理
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOCIAL_CONTENT_KEYS = [
    "caption", "text_raw", "text", "desc", "description", "content", "body",
    "title", "note", "status", "message", "blog", "article",
]
SOCIAL_TIME_KEYS = [
    "created_at", "creation_timestamp", "timestamp", "create_time", "publishtime",
    "publish_time", "posted_at", "taken_at", "time", "date", "datetime", "ctime",
]
SOCIAL_AUTHOR_KEYS = ["user", "username", "author", "nickname", "owner", "name", "screen_name"]
TAG_RE = re.compile(r"<[^>]+>")

APPLE_EPOCH = 978307200  # 2001-01-01 UTC in unix seconds


def _norm(key: Any) -> str:
    return re.sub(r"[\s_\-]", "", str(key)).lower()


def _find(row: dict, keys: list[str]) -> Any:
    lookup = {_norm(k): k for k in row.keys()}
    for key in keys:
        real = lookup.get(_norm(key))
        if real is not None and row[real] not in (None, ""):
            return row[real]
    return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _strip_html(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return html.unescape(TAG_RE.sub("", value)).strip()


def _ts(value: Any) -> str | None:
    """把各种时间表示统一成可读字符串。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e18:      # nanoseconds
            number /= 1e9
        elif number > 1e14:    # microseconds
            number /= 1e6
        elif number > 1e11:    # milliseconds
            number /= 1e3
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return _clean(value)


def _ms(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return _clean(value)


# --------------------------------------------------------------------------- #
# 社交平台导出（微博 / 豆瓣 / 小红书 / Instagram 等通用 JSON）
# --------------------------------------------------------------------------- #

def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def parse_social_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for node in _walk(data):
        raw = _find(node, SOCIAL_CONTENT_KEYS)
        if not isinstance(raw, str):
            continue
        text = _strip_html(raw)
        if not text or len(text) < 2:
            continue
        time_value = _ts(_find(node, SOCIAL_TIME_KEYS))
        author = _find(node, SOCIAL_AUTHOR_KEYS)
        if isinstance(author, dict):
            author = _find(author, SOCIAL_AUTHOR_KEYS)
        author = _clean(author)
        key = (text[:200], str(time_value))
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "time": time_value, "content": text, "sender": author,
            "subject": True, "kind": "post",
        })
    return items


# --------------------------------------------------------------------------- #
# iMessage（macOS chat.db / SQLite）
# --------------------------------------------------------------------------- #

def parse_imessage_sqlite(path: Path) -> list[dict[str, Any]]:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ValueError(f"无法打开数据库 {path}: {error}") from error
    con.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "message" not in tables:
            raise ValueError(f"{path} 不是 iMessage 数据库（缺少 message 表）")
        query = (
            "SELECT m.date AS date, m.is_from_me AS is_from_me, m.text AS text, "
            "h.id AS handle "
            "FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID "
            "ORDER BY m.date"
        )
        rows = con.execute(query).fetchall()
    except sqlite3.Error as error:
        raise ValueError(f"读取 iMessage 数据库失败: {error}") from error
    finally:
        con.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        text = _clean(row["text"])
        if not text:
            continue
        stamp = row["date"]
        if stamp:
            seconds = stamp / 1e9 if stamp > 1e12 else stamp
            time_value = datetime.fromtimestamp(APPLE_EPOCH + seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            time_value = None
        is_me = bool(row["is_from_me"])
        items.append({
            "time": time_value,
            "sender": "我" if is_me else _clean(row["handle"]) or "对方",
            "content": text,
        })
    return items


# --------------------------------------------------------------------------- #
# Android 短信备份（SMS Backup & Restore XML）
# --------------------------------------------------------------------------- #

def parse_sms_xml(path: Path) -> list[dict[str, Any]]:
    import xml.etree.ElementTree as ET

    root = ET.parse(str(path)).getroot()
    items: list[dict[str, Any]] = []
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag not in {"sms", "mms"}:
            continue
        body = node.get("body") or node.get("text") or ""
        if not body and tag == "mms":
            body = " ".join(
                part.get("text", "") for part in node.iter()
                if part.tag.split("}")[-1].lower() == "part" and part.get("text")
            )
        body = _clean(body)
        if not body:
            continue
        address = node.get("address") or node.get("contact_name") or "对方"
        kind = node.get("type")
        is_me = kind in {"2", "3", "4"}  # sent / draft / outbox
        date = node.get("date") or node.get("date_sent")
        items.append({
            "time": _ms(date),
            "sender": "我" if is_me else address,
            "content": body,
        })
    return items


# --------------------------------------------------------------------------- #
# PDF（先抽文本，能识别成聊天就按聊天解析，否则当作一段独白）
# --------------------------------------------------------------------------- #

def parse_pdf(path: Path) -> list[dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover
        raise ValueError("解析 PDF 需要 pypdf，请执行 pip install pypdf") from error

    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = text.strip()
    if not text:
        return []

    from .ingest import looks_like_chat, parse_txt_text

    if looks_like_chat(text):
        return parse_txt_text(text)
    return [{"content": text, "subject": True, "kind": "post"}]


# --------------------------------------------------------------------------- #
# 照片（EXIF 时间线 + 可选同名 .txt 配文）
# --------------------------------------------------------------------------- #

def _dms(value: Any) -> float | None:
    try:
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return float(value[0]) + float(value[1]) / 60 + float(value[2]) / 3600
        return float(value)
    except (TypeError, ValueError, IndexError):
        return None


def read_exif(path: Path) -> dict[str, str]:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return {}
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return {}
            exif_ifd = {}
            gps_ifd = {}
            try:
                exif_ifd = exif.get_ifd(0x8769) or {}
                gps_ifd = exif.get_ifd(0x8825) or {}
            except Exception:  # noqa: BLE001
                pass
            meta: dict[str, str] = {}
            stamp = exif_ifd.get(36867) or exif_ifd.get(36868) or exif.get(306)
            if stamp:
                meta["datetime"] = str(stamp).replace(":", "-", 2)
            desc = exif.get(270) or exif_ifd.get(270)
            if desc:
                meta["description"] = str(desc)
            make = exif.get(271) or ""
            model = exif.get(272) or ""
            camera = f"{make} {model}".strip()
            if camera:
                meta["camera"] = camera
            lat = _dms(gps_ifd.get(2))
            lon = _dms(gps_ifd.get(4))
            if lat is not None and lon is not None:
                if gps_ifd.get(1) == "S":
                    lat = -lat
                if gps_ifd.get(3) == "W":
                    lon = -lon
                meta["place"] = f"{lat:.4f},{lon:.4f}"
            return meta
    except Exception:  # noqa: BLE001
        return {}


def parse_image(path: Path) -> list[dict[str, Any]]:
    meta = read_exif(path)
    caption = ""
    sidecar = path.with_suffix(".txt")
    if sidecar.exists():
        caption = _clean(sidecar.read_text(encoding="utf-8", errors="ignore"))

    parts: list[str] = []
    if caption:
        parts.append(caption)
    if meta.get("description"):
        parts.append(meta["description"])
    if meta.get("camera"):
        parts.append(meta["camera"])
    if meta.get("place"):
        parts.append(f"地点 {meta['place']}")

    if not parts:
        return []
    return [{
        "time": meta.get("datetime"),
        "content": "[照片] " + "，".join(parts),
        "subject": True,
        "kind": "photo",
    }]
