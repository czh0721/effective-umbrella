import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import sources

SELF_ALIASES = {"我", "me", "self", "myself", "自己", "本人"}
DEFAULT_OTHER = "对方"

SOCIAL_SUFFIXES = {".json", ".jsonl"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
XML_SUFFIXES = {".xml"}
PDF_SUFFIXES = {".pdf"}
HTML_SUFFIXES = {".html", ".htm"}

TIME_KEYS = [
    "time", "timestamp", "date", "datetime", "created_at", "createtime",
    "strtime", "str_time", "msgtime", "时间", "日期", "发送时间",
]
SENDER_KEYS = [
    "sender", "talker", "nickname", "from", "speaker", "username", "name",
    "fromuser", "sendername", "发送者", "昵称", "用户名", "联系人", "说话人", "姓名",
]
CONTENT_KEYS = [
    "content", "text", "message", "msg", "body", "strcontent", "str_content",
    "内容", "消息", "正文", "文本", "聊天内容",
]
IS_SELF_KEYS = [
    "issender", "is_sender", "isfromme", "issend", "direction", "发送方", "是否本人",
]

TS_RE = re.compile(
    r"^(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?[ T]*\d{1,2}:\d{2}(?::\d{2})?)\s*(.*)$"
)


@dataclass
class Message:
    time: str | None
    sender: str
    text: str
    is_target: bool = False
    subject: bool = False
    kind: str = "text"


@dataclass
class IngestResult:
    messages: list[Message] = field(default_factory=list)
    target: str | None = None
    sources: list[str] = field(default_factory=list)


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-]", "", str(key)).lower()


def _pick(row: dict[str, Any], keys: list[str]) -> tuple[str | None, Any]:
    if not isinstance(row, dict):
        return None, None
    lookup = {_norm_key(k): k for k in row.keys()}
    for key in keys:
        real = lookup.get(_norm_key(key))
        if real is not None and row[real] not in (None, ""):
            return real, row[real]
    return None, None


def _clean(text: Any) -> str:
    if text is None:
        return ""
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip()


def _is_self(value: Any) -> bool | None:
    if value is None:
        return None
    token = _norm_key(str(value))
    if token in {"1", "true", "yes", "y", "是", "self", "out", "outgoing", "send", "sent"}:
        return True
    if token in {"0", "false", "no", "n", "否", "other", "in", "incoming", "recv", "receive"}:
        return False
    return None


def looks_like_chat(text: str) -> bool:
    for line in text.splitlines():
        if TS_RE.match(line.strip()):
            return True
    return False


def parse_txt_text(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        if current is None:
            return
        content = _clean("\n".join(current["lines"]))
        if current["sender"] and content:
            items.append(
                {"time": current["time"], "sender": current["sender"], "content": content}
            )

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = TS_RE.match(line)
        if match:
            flush()
            timestamp, rest = match.group(1), match.group(2).strip()
            current = {"time": timestamp, "sender": None, "lines": []}
            if rest:
                if ":" in rest or "：" in rest:
                    sep = ":" if ":" in rest else "："
                    speaker, _, content = rest.partition(sep)
                    if 0 < len(speaker.strip()) <= 30:
                        current["sender"] = speaker.strip()
                        current["lines"].append(content.strip())
                    else:
                        current["sender"] = rest
                else:
                    current["sender"] = rest
            continue
        if current is None:
            continue
        if current["sender"] is None:
            current["sender"] = line
        else:
            current["lines"].append(line)
    flush()
    return items


def parse_txt(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    items = parse_txt_text(text)
    if not items:
        content = _clean(text)
        if content:
            return [{"content": content, "subject": True, "kind": "post"}]
    return items


def parse_csv(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            items.append(dict(row))
    return items


def _iter_json_objects(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, (dict, list))]
    if isinstance(data, dict):
        for key in ("messages", "data", "list", "records", "items", "msg", "chat"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, (dict, list))]
        return [data]
    return []


def parse_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    rows = _iter_json_objects(data)
    if _rows_have_chat_keys(rows):
        return rows
    posts = sources.parse_social_json(path)
    return posts or rows


def _rows_have_chat_keys(rows: list[dict[str, Any]]) -> bool:
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        _, content_value = _pick(row, CONTENT_KEYS)
        _, sender_value = _pick(row, SENDER_KEYS)
        _, self_value = _pick(row, IS_SELF_KEYS)
        if not content_value:
            continue
        if self_value is not None:
            return True
        if isinstance(sender_value, str) and sender_value.strip():
            return True
    return False


def _sniff_database(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def detect_and_load(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return sources.parse_image(path)
    if suffix in PDF_SUFFIXES:
        return sources.parse_pdf(path)
    if suffix in SQLITE_SUFFIXES or _sniff_database(path):
        return sources.parse_imessage_sqlite(path)
    if suffix in XML_SUFFIXES:
        return sources.parse_sms_xml(path)
    if suffix in SOCIAL_SUFFIXES:
        return parse_json(path)
    if suffix in HTML_SUFFIXES:
        return sources.parse_social_json(path)
    if suffix == ".csv":
        return parse_csv(path)
    if suffix in {".txt", ".log", ".md"}:
        return parse_txt(path)

    # 未知扩展名：按内容特征判断
    if _sniff_database(path):
        return sources.parse_imessage_sqlite(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            rows = _iter_json_objects(json.loads(text))
            if not _rows_have_chat_keys(rows):
                posts = sources.parse_social_json(path)
                if posts:
                    return posts
            return rows
        except json.JSONDecodeError:
            pass
    if stripped.startswith("<?xml") or stripped.startswith("<sms"):
        try:
            return sources.parse_sms_xml(path)
        except Exception:  # noqa: BLE001
            pass
    lines = text.splitlines()
    if lines and "," in lines[0]:
        try:
            return parse_csv(path)
        except Exception:  # noqa: BLE001
            pass
    return parse_txt(path)


def normalize(raw_items: list[dict[str, Any]]) -> list[Message]:
    messages: list[Message] = []
    for item in raw_items:
        if isinstance(item, list):
            item = {"time": item[0] if len(item) > 0 else None,
                    "sender": item[1] if len(item) > 1 else None,
                    "content": item[2] if len(item) > 2 else None}
        _, time_value = _pick(item, TIME_KEYS)
        _, sender_value = _pick(item, SENDER_KEYS)
        _, content_value = _pick(item, CONTENT_KEYS)
        _, self_value = _pick(item, IS_SELF_KEYS)
        subject = bool(item.get("subject")) if isinstance(item, dict) else False
        kind = _clean(item.get("kind")) or "text" if isinstance(item, dict) else "text"

        sender = _clean(sender_value)
        content = _clean(content_value)
        if not content:
            continue
        self_flag = _is_self(self_value)
        if self_flag is True:
            sender = "我"
        elif self_flag is False and (not sender or sender in SELF_ALIASES):
            sender = DEFAULT_OTHER
        elif not sender:
            sender = DEFAULT_OTHER
        messages.append(
            Message(
                time=_clean(time_value) or None,
                sender=sender,
                text=content,
                subject=subject,
                kind=kind,
            )
        )
    return messages


def select_target(messages: list[Message], target: str | None) -> str | None:
    if target:
        for message in messages:
            if message.subject:
                message.is_target = True
                if not message.sender or message.sender in SELF_ALIASES:
                    message.sender = target
            else:
                message.is_target = message.sender == target
        return target
    counts = Counter(
        message.sender
        for message in messages
        if message.sender and message.sender not in SELF_ALIASES and message.sender != DEFAULT_OTHER
    )
    if not counts:
        counts = Counter(
            message.sender for message in messages if message.sender not in SELF_ALIASES
        )
    if not counts:
        counts = Counter(message.sender for message in messages)
    if not counts:
        for message in messages:
            message.is_target = True
        return None
    chosen = counts.most_common(1)[0][0]
    for message in messages:
        if message.subject:
            message.is_target = True
            if not message.sender or message.sender in SELF_ALIASES:
                message.sender = chosen
        else:
            message.is_target = message.sender == chosen
    return chosen


def collect_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    allowed = (
        {".txt", ".csv", ".json", ".jsonl", ".log", ".md"}
        | SOCIAL_SUFFIXES | IMAGE_SUFFIXES | SQLITE_SUFFIXES
        | XML_SUFFIXES | PDF_SUFFIXES | HTML_SUFFIXES
    )
    files: list[Path] = []
    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in allowed:
            files.append(candidate)
    return files


def parse_text_blob(text: str) -> list[dict[str, Any]]:
    content = _clean(text)
    if not content:
        return []
    items = parse_txt_text(content)
    if items:
        return items
    return [{"content": content, "subject": True, "kind": "post"}]


def run(
    input_path: Path | None,
    out_dir: Path,
    target: str | None,
    extra_text: str | None = None,
) -> IngestResult:
    files = collect_files(input_path) if input_path is not None else []
    if not files and not (extra_text and extra_text.strip()):
        raise FileNotFoundError(f"未在 {input_path} 找到可解析的文件")

    all_messages: list[Message] = []
    for file in files:
        all_messages.extend(normalize(detect_and_load(file)))
    if extra_text and extra_text.strip():
        all_messages.extend(normalize(parse_text_blob(extra_text)))

    if not all_messages:
        raise ValueError("解析后没有得到任何消息，请检查文件格式或列名")

    chosen = select_target(all_messages, target)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "messages.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for message in all_messages:
            handle.write(json.dumps(asdict(message), ensure_ascii=False) + "\n")

    counter = Counter(message.sender for message in all_messages)
    kind_counter = Counter(message.kind for message in all_messages)
    meta = {
        "target": chosen,
        "total_messages": len(all_messages),
        "target_messages": sum(1 for m in all_messages if m.is_target),
        "kinds": dict(kind_counter.most_common()),
        "senders": dict(counter.most_common()),
        "sources": [str(file) for file in files] + (["<pasted text>"] if extra_text and extra_text.strip() else []),
    }
    (out_dir / "ingest_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return IngestResult(messages=all_messages, target=chosen,
                        sources=meta["sources"])


def read_messages(path: Path) -> list[Message]:
    messages: list[Message] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            messages.append(Message(**json.loads(line)))
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析并归一化多平台聊天记录")
    parser.add_argument("--input", default=None, help="聊天记录文件或目录")
    parser.add_argument("--out", default="data/profile", help="输出目录")
    parser.add_argument("--target", default=None, help="要复刻的对象昵称，缺省自动推断")
    parser.add_argument("--text", default=None, help="直接追加的文本（描述或粘贴的记录）")
    args = parser.parse_args(argv)

    result = run(Path(args.input) if args.input else None, Path(args.out), args.target, args.text)
    print(f"解析完成: {len(result.messages)} 条消息，目标对象 = {result.target}")
    print(f"输出: {Path(args.out) / 'messages.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
