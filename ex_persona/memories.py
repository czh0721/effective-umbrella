"""长期记忆：从对话中抽取关键信息，并在回复前按话题召回。

- 抽取：调用用户配置的大模型，把若干轮对话归纳为结构化记忆；
- 召回：复用 :class:`retrieval.BM25` 做关键词检索，不依赖 embedding 服务；
- 隔离：记忆以 ``(persona_id, contact_key)`` 为边界，同一人格下不同聊天对象互不可见。
"""

import json
import re

from .config import LLMConfig
from .llm import chat
from .retrieval import BM25, tokenize

VALID_KINDS = {"fact", "preference", "event", "relation"}
MAX_CONTENT = 200

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    """中文分词 + 汉字二元组，避免 jieba 把「咖啡」并进「喝咖啡」而漏召回。"""
    text = text or ""
    tokens = tokenize(text)
    for run in _CJK_RUN_RE.findall(text):
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens

EXTRACT_SYSTEM = """你是一个对话记忆抽取器。请从给定的聊天记录里提取值得长期记住的信息。

要求：
1. 只提取关于「{name}」或双方关系的事实、偏好、事件、承诺等有价值的信息。
2. 不要提取寒暄、语气词、系统提示词、回复模板等内容。
3. 每条信息独立成条，不超过 {limit} 字，用简洁的陈述句。
4. kind 取值：fact（事实）、preference（偏好）、event（事件）、relation（关系）。
5. 只输出 JSON，不要任何解释或 Markdown 代码块。

输出格式：
{{"memories": [{{"kind": "fact", "content": "..."}}]}}

如果没有值得记住的信息，输出 {{"memories": []}}。"""


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’()（）\[\]【】]", "", text or "").lower()


def _clean_content(text: object) -> str:
    if not isinstance(text, str):
        return ""
    value = re.sub(r"\s+", " ", text).strip()
    value = value.strip("。.，,；;：:")
    if len(value) < 2:
        return ""
    return value[:MAX_CONTENT]


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_items(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("memories", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def parse(raw: str) -> list[dict]:
    """解析模型返回的 JSON；任何格式异常都返回空列表。"""
    text = _strip_fences(raw)
    if not text:
        return []
    payload: object = None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        match = re.search(r"[\[{].*[\]}]", text, re.S)
        if match:
            try:
                payload = json.loads(match.group(0))
            except (ValueError, TypeError):
                payload = None
    items = _extract_items(payload)
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _clean_content(item.get("content") or item.get("text"))
        if not content:
            continue
        kind = item.get("kind") or item.get("type") or "fact"
        kind = kind if kind in VALID_KINDS else "fact"
        result.append({"kind": kind, "content": content})
    return result


def build_messages(persona_name: str, turns: list[dict], existing: list[str]) -> list[dict]:
    lines = []
    for turn in turns:
        speaker = "对方" if turn.get("role") == "user" else persona_name
        content = _clean_content(turn.get("content"))
        if content:
            lines.append(f"{speaker}：{content}")
    known = "\n".join(f"- {text}" for text in existing[:80]) or "（暂无）"
    user = (
        f"【已知记忆】\n{known}\n\n"
        f"【新聊天记录】\n" + "\n".join(lines)
    )
    return [
        {"role": "system", "content": EXTRACT_SYSTEM.format(name=persona_name, limit=MAX_CONTENT)},
        {"role": "user", "content": user},
    ]


def extract(
    config: LLMConfig,
    persona_name: str,
    turns: list[dict],
    existing: list[str] | None = None,
) -> list[dict] | None:
    """调用模型抽取记忆。

    返回 ``None`` 表示调用失败（调用方应保留轮次待下次重试）；返回 ``[]`` 表示
    调用成功但本轮没有值得记住的信息。
    """
    if not config or not getattr(config, "ready", False) or not turns:
        return None
    existing = existing or []
    known = {_normalize(text) for text in existing}
    try:
        raw = chat(
            config,
            build_messages(persona_name, turns, existing),
            temperature=0.2,
            max_tokens=800,
            json_mode=True,
        )
    except Exception:
        return None
    fresh: list[dict] = []
    for item in parse(raw):
        key = _normalize(item["content"])
        if not key or key in known:
            continue
        known.add(key)
        fresh.append(item)
    return fresh


def recall(items: list[dict], query: str, k: int = 6) -> list[dict]:
    """按关键词检索最相关的记忆；得分为 0 的条目会被过滤。"""
    if not items or not (query or "").strip():
        return []
    docs = [item.get("content") or "" for item in items]
    index = BM25(docs, tokenizer=_tokenize)
    hits = index.top_k(query, k)
    return [items[index_] for index_, _ in hits]


def format_block(items: list[dict], persona_name: str) -> str:
    """把召回的长期记忆格式化为注入系统提示词的区块。"""
    if not items:
        return ""
    label = {"fact": "事实", "preference": "偏好", "event": "事件", "relation": "关系"}
    lines = [f"- [{label.get(item.get('kind'), '记忆')}] {item.get('content')}" for item in items]
    return (
        f"【关于对方的长期记忆】\n"
        f"以下是你（{persona_name}）以前记住的关于对方的信息，"
        f"回复时可以自然地体现，但不要直接背诵：\n" + "\n".join(lines)
    )
