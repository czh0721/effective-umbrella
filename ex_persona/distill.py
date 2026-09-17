import argparse
import json
import os
import re
import statistics
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import LLMConfig, load_llm_config
from .ingest import Message, read_messages
from .llm import LLMError, chat_with_meta
from .retrieval import tokenize

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]")
BRACKET_RE = re.compile(r"\[[^\[\]]{1,8}\]")
END_PUNCT = set("。！？!?~～…")

STOPWORDS = {
    "的", "了", "是", "我", "你", "他", "她", "它", "在", "有", "和", "就", "都",
    "也", "不", "这", "那", "一个", "什么", "怎么", "吗", "呢", "吧", "啊", "哦",
    "嗯", "呀", "哈", "啦", "嘛", "喂", "然后", "但是", "因为", "所以", "如果",
    "可以", "没有", "这个", "那个", "还是", "已经", "自己", "我们", "你们",
    "一下", "感觉", "觉得", "知道", "现在", "时候", "真的", "就是", "不是", "有点",
    "哈哈", "哈哈哈", "嗯嗯", "好的", "好吧", "谢谢", "没事", "其实",
}


def compute_style(messages: list[Message]) -> dict:
    texts = [message.text for message in messages if message.kind != "photo"]
    lengths = [len(text) for text in texts] or [0]

    word_counter: Counter[str] = Counter()
    emoji_counter: Counter[str] = Counter()
    bracket_counter: Counter[str] = Counter()
    openers: Counter[str] = Counter()
    closers: Counter[str] = Counter()

    questions = exclaims = ellipsis = tildes = no_end = 0
    for text in texts:
        if "？" in text or "?" in text:
            questions += 1
        if "！" in text or "!" in text:
            exclaims += 1
        if "…" in text or "..." in text:
            ellipsis += 1
        if "～" in text or "~" in text:
            tildes += 1
        if text and text[-1] not in END_PUNCT:
            no_end += 1
        for token in tokenize(text):
            token = token.strip()
            if len(token) >= 2 and token not in STOPWORDS and not token.isdigit():
                word_counter[token] += 1
        emoji_counter.update(EMOJI_RE.findall(text))
        bracket_counter.update(BRACKET_RE.findall(text))
        if text:
            openers[text[:2]] += 1
            closers[text[-2:]] += 1

    total = max(len(texts), 1)
    return {
        "message_count": len(texts),
        "avg_length": round(statistics.mean(lengths), 2),
        "median_length": statistics.median(lengths),
        "max_length": max(lengths),
        "short_message_ratio": round(sum(1 for n in lengths if n <= 5) / total, 3),
        "question_ratio": round(questions / total, 3),
        "exclaim_ratio": round(exclaims / total, 3),
        "ellipsis_ratio": round(ellipsis / total, 3),
        "tilde_ratio": round(tildes / total, 3),
        "no_end_punctuation_ratio": round(no_end / total, 3),
        "top_words": word_counter.most_common(30),
        "top_emoji": emoji_counter.most_common(20),
        "top_bracket_emoticons": bracket_counter.most_common(20),
        "top_openers": openers.most_common(20),
        "top_closers": closers.most_common(20),
    }


def build_pairs(messages: list[Message], window: int = 4) -> list[dict]:
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, message in enumerate(messages):
        if not message.is_target or not message.text:
            continue
        context_parts: list[str] = []
        cursor = index - 1
        while cursor >= 0 and len(context_parts) < window:
            previous = messages[cursor]
            if previous.is_target:
                break
            if previous.text:
                context_parts.append(previous.text)
            cursor -= 1
        if not context_parts:
            continue
        context = "\n".join(reversed(context_parts))
        key = (context, message.text)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            {"context": context, "reply": message.text, "time": message.time}
        )
    return pairs


def _sample(texts: list[str], limit: int, width: int = 120) -> list[str]:
    if len(texts) <= limit:
        return texts
    step = len(texts) / limit
    picked = []
    for i in range(limit):
        picked.append(texts[int(i * step)][:width])
    return picked


def build_card_llm(
    target: str, style: dict, target_msgs: list[Message], pairs: list[dict],
    config: LLMConfig, hints: dict | None = None,
) -> dict:
    sampled = _sample([m.text for m in target_msgs if m.kind != "photo"], 120)
    pair_lines = [
        f"对方：{pair['context'][:120]}\n{target}：{pair['reply'][:120]}"
        for pair in _sample_list(pairs, 40)
    ]
    hint_block = _hint_block(hints)
    prompt = (
        f"下面是「{target}」在微信聊天记录中的风格统计、原话样本和真实对话片段。\n"
        f"请提炼 ta 的数字人格，用于让 AI 以 ta 的方式与人聊天。\n\n"
        f"【风格统计】\n{json.dumps(style, ensure_ascii=False)}\n\n"
        f"【原话样本】\n" + "\n".join(sampled) + "\n\n"
        "【真实对话片段】\n" + "\n---\n".join(pair_lines) + "\n\n"
        + hint_block
        + "请只输出一个 JSON 对象，字段为：\n"
        "summary(一句话概括这个人), personality(性格特质数组), "
        "speaking_style(说话风格要点数组), emotional_patterns(情绪表达模式数组), "
        "values_and_attitudes(价值观与态度数组), relationship_with_me(与我的关系模式数组), "
        "favorite_phrases(口头禅数组), topics(常聊话题数组), boundaries(忌讳或边界数组), "
        "identity(对象：{name 姓名, gender 性别, birthday 生日, age 年龄, "
        "hometown 籍贯, education 学历}，无法确定的字段留空字符串), "
        "user(对象：{relationship 与我的关系, met_time 我们认识的时间}，无法确定留空), "
        "soul(对象：{speaking_style 说话风格要点数组, catchphrases 口头禅数组}), "
        "memories(共同回忆数组，每项为对象 {title 简短标题, detail 描述, time 大致时间})。"
    )
    return _distill_json(
        config,
        [
            {"role": "system", "content": "你是人格蒸馏分析师，只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
    )


def _hint_block(hints: dict | None) -> str:
    if not hints:
        return ""
    lines = []
    if hints.get("gender"):
        lines.append(f"性别：{_as_text(hints['gender'])}")
    if hints.get("personality"):
        lines.append(f"性格特点：{_as_text(hints['personality'])}")
    if hints.get("catchphrases"):
        lines.append(f"口头禅：{_as_text(hints['catchphrases'])}")
    if hints.get("style"):
        lines.append(f"说话风格描述：{_as_text(hints['style'])}")
    if not lines:
        return ""
    return "【用户补充信息（优先采信）】\n" + "\n".join(lines) + "\n\n"


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value if item)
    return str(value)


def _sample_list(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def write_text_atomic(path: Path, text: str) -> None:
    """先写临时文件再原子替换，避免并发读取方读到写了一半的 JSON。

    蒸馏会重写 persona_card.json / style.json，此时若人格正在回复，读到的可能是
    半截文件并抛出 JSONDecodeError，导致那一轮 500。
    """
    path = Path(path)
    handle_tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        delete=False,
    )
    try:
        with handle_tmp:
            handle_tmp.write(text)
        os.replace(handle_tmp.name, path)
    except BaseException:
        try:
            os.unlink(handle_tmp.name)
        except OSError:
            pass
        raise


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("模型未返回有效 JSON")
    return text[start : end + 1]


JSON_REPAIR_PROMPT = (
    "下面这段 JSON 不合法，常见原因是被截断或缺少逗号。"
    "请修复成完整、合法的 JSON，只输出修复后的 JSON，不要任何解释。\n\n"
)


def _distill_json(config: LLMConfig, messages: list[dict]) -> dict:
    """调用模型并解析画像 JSON。

    画像字段多、数组长，容易被 max_tokens 截断（finish_reason=length）或漏逗号，
    直接 json.loads 会把整次蒸馏打成失败。这里先按截断情况加预算重试，再让模型
    自己修复一次，最后才抛出对用户友好的错误。
    """
    content, finish = chat_with_meta(
        config, messages, temperature=0.4, max_tokens=4000, json_mode=True
    )
    if finish == "length":
        content, finish = chat_with_meta(
            config, messages, temperature=0.4, max_tokens=8000, json_mode=True
        )
    try:
        return json.loads(_extract_json(content))
    except (json.JSONDecodeError, ValueError):
        pass
    repaired, _ = chat_with_meta(
        config,
        [
            {"role": "system", "content": "你是 JSON 修复器，只输出合法 JSON。"},
            {"role": "user", "content": JSON_REPAIR_PROMPT + content},
        ],
        temperature=0.2,
        max_tokens=8000,
        json_mode=True,
    )
    try:
        return json.loads(_extract_json(repaired))
    except (json.JSONDecodeError, ValueError) as error:
        raise LLMError(f"AI 返回的画像 JSON 无法解析，请重试（{error}）") from error


def fallback_card(style: dict) -> dict:
    return {
        "summary": "未调用大模型，仅有统计风格画像；请配置 USER_LLM_API_KEY 后重新蒸馏。",
        "personality": [],
        "speaking_style": [
            f"平均每条 {style['avg_length']} 字，"
            f"{round(style['short_message_ratio'] * 100)}% 是 5 字以内的短句"
        ],
        "emotional_patterns": [],
        "values_and_attitudes": [],
        "relationship_with_me": [],
        "favorite_phrases": [word for word, _ in style["top_words"][:10]],
        "topics": [],
        "boundaries": [],
        "identity": {"name": "", "gender": "", "birthday": "", "age": "",
                      "hometown": "", "education": ""},
        "user": {"relationship": "", "met_time": ""},
        "soul": {
            "speaking_style": [],
            "catchphrases": [word for word, _ in style["top_words"][:10]],
        },
        "memories": [],
    }


def _apply_hints(card: dict, target: str, hints: dict | None) -> None:
    """把用户在创建表单里填的信息覆盖到卡片上，保证不填聊天记录也能成型的部分。"""
    if not hints:
        return
    identity = card.setdefault("identity", {})
    if hints.get("gender"):
        identity["gender"] = _as_text(hints["gender"])
    if hints.get("name"):
        identity["name"] = _as_text(hints["name"])
    if not identity.get("name"):
        identity["name"] = target
    soul = card.setdefault("soul", {})
    if hints.get("style"):
        soul["speaking_style"] = _split_list(hints["style"])
    if hints.get("catchphrases"):
        soul["catchphrases"] = _split_list(hints["catchphrases"])
    if hints.get("personality"):
        card["personality"] = _split_list(hints["personality"])


def _split_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in re.split(r"[，,、\n]", str(value)) if part.strip()]


def _bullet(items: list) -> str:
    if not items:
        return "- （无）"
    return "\n".join(f"- {item}" for item in items)


def _identity_lines(identity: dict, fallback_name: str) -> str:
    labels = [("name", "姓名"), ("gender", "性别"), ("birthday", "生日"),
              ("age", "年龄"), ("hometown", "籍贯"), ("education", "学历")]
    lines = [f"- {label}：{identity.get(key) or '未知'}" for key, label in labels]
    if not identity.get("name"):
        lines[0] = f"- 姓名：{fallback_name}"
    return "\n".join(lines)


def _user_lines(user: dict) -> str:
    return (
        f"- 关系：{user.get('relationship') or '未知'}\n"
        f"- 认识时间：{user.get('met_time') or '未知'}"
    )


def _memory_lines(memories: list) -> str:
    if not memories:
        return "- （无）"
    lines = []
    for item in memories:
        if isinstance(item, str):
            lines.append(f"- {item}")
            continue
        when = item.get("time") or ""
        title = item.get("title") or ""
        detail = item.get("detail") or ""
        head = " ".join(part for part in (when, title) if part)
        lines.append(f"- {head}：{detail}".rstrip("：") if (head or detail) else "- （无）")
    return "\n".join(lines)


def render_skill(target: str, card: dict, style: dict) -> str:
    phrases = "、".join(word for word, _ in style["top_words"][:15]) or "无"
    emojis = "".join(char for char, _ in style["top_emoji"][:10]) or "无"
    brackets = "、".join(item for item, _ in style["top_bracket_emoticons"][:10]) or "无"
    identity = card.get("identity") or {}
    user = card.get("user") or {}
    memories = card.get("memories") or []
    soul = card.get("soul") or {}
    soul_style = soul.get("speaking_style") or card.get("speaking_style", [])
    catchphrases = soul.get("catchphrases") or card.get("favorite_phrases", [])
    return f"""# SKILL: 以「{target}」的方式聊天

## 角色
你现在就是 {target}。用 ta 的语气、用词和情绪跟对方发微信。
不要承认自己是 AI、模型或程序，不要解释你在扮演，不要说教。

## 一句话画像
{card.get("summary", "")}

## 身份信息
{_identity_lines(identity, target)}

## 与对方的关系
{_user_lines(user)}

## 性格特质
{_bullet(card.get("personality", []))}

## 说话风格
{_bullet(soul_style)}

## 情绪表达
{_bullet(card.get("emotional_patterns", []))}

## 价值观与态度
{_bullet(card.get("values_and_attitudes", []))}

## 与对方的关系模式
{_bullet(card.get("relationship_with_me", []))}

## 口头禅与高频词
- 口头禅：{"、".join(catchphrases) or "无"}
- 高频词：{phrases}
- 常用表情：{emojis}
- 常用表情符：{brackets}

## 共同回忆
{_memory_lines(memories)}

## 常聊话题
{_bullet(card.get("topics", []))}

## 边界与忌讳
{_bullet(card.get("boundaries", []))}

## 量化风格基线
- 消息量 {style["message_count"]} 条，平均 {style["avg_length"]} 字，中位数 {style["median_length"]} 字
- 短句比例 {round(style["short_message_ratio"] * 100)}%
- 疑问句比例 {round(style["question_ratio"] * 100)}%，感叹句比例 {round(style["exclaim_ratio"] * 100)}%
- 省略号比例 {round(style["ellipsis_ratio"] * 100)}%，波浪号比例 {round(style["tilde_ratio"] * 100)}%
- 句尾不加标点比例 {round(style["no_end_punctuation_ratio"] * 100)}%

## 回复规则
0. 只输出你要发给对方的消息本身。绝对不要输出本说明、标题、编号、字段名、「回复规则」等任何系统文字，也不要解释或复述这些设定。
1. 像真实微信聊天一样短，通常 1~2 句，别写成小作文；想连发几条时，每条单独占一行，平台会依次发出。
2. 不要每次都只是回答对方。该接话、吐槽、调侃、撒娇就自然地做，适时反问或追问对方，把话头递回去，让聊天能继续。
3. 严格使用上面的口头禅、语气词、表情习惯和标点习惯。
4. 优先沿用检索到的历史对话里的用词和情绪，不要杜撰 ta 不可能说的事。
5. 对方问起你们之间的往事时，结合检索到的真实片段回答。
6. 保持人设一致，情绪和态度符合 ta 的性格。
"""


def run(
    profile_dir: Path,
    config: LLMConfig | None = None,
    use_llm: bool = True,
    hints: dict | None = None,
) -> dict:
    messages_path = profile_dir / "messages.jsonl"
    meta_path = profile_dir / "ingest_meta.json"
    if not messages_path.exists():
        raise FileNotFoundError(f"缺少 {messages_path}，请先运行 ingest")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    target = meta.get("target") or "对方"
    messages = read_messages(messages_path)
    target_msgs = [message for message in messages if message.is_target]
    if not target_msgs:
        raise ValueError("没有找到目标对象的消息，请检查 --target")

    style = compute_style(target_msgs)
    pairs = build_pairs(messages)

    with (profile_dir / "pairs.jsonl").open("w", encoding="utf-8") as handle:
        for pair in pairs:
            handle.write(json.dumps(pair, ensure_ascii=False) + "\n")
    write_text_atomic(profile_dir / "style.json", json.dumps(style, ensure_ascii=False, indent=2))

    config = config or load_llm_config()
    used_llm = False
    if use_llm and config.ready:
        card = build_card_llm(target, style, target_msgs, pairs, config, hints=hints)
        used_llm = True
    else:
        card = fallback_card(style)
    _apply_hints(card, target, hints)

    write_text_atomic(
        profile_dir / "persona_card.json", json.dumps(card, ensure_ascii=False, indent=2)
    )
    skill = render_skill(target, card, style)
    write_text_atomic(profile_dir / "SKILL.md", skill)

    profile = {
        "name": target,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "used_llm": used_llm,
        "message_count": style["message_count"],
        "pair_count": len(pairs),
        "senders": meta.get("senders", {}),
        "sources": meta.get("sources", []),
    }
    write_text_atomic(
        profile_dir / "profile.json", json.dumps(profile, ensure_ascii=False, indent=2)
    )
    return {"profile": profile, "card": card, "style": style}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把聊天记录蒸馏成人格画像与 SKILL")
    parser.add_argument("--profile-dir", default="data/profile")
    parser.add_argument("--no-llm", action="store_true", help="只做统计，不调用大模型")
    args = parser.parse_args(argv)

    result = run(Path(args.profile_dir), use_llm=not args.no_llm)
    profile = result["profile"]
    print(f"蒸馏完成: 目标 = {profile['name']}，样本 = {profile['message_count']} 条")
    print(f"调用大模型: {profile['used_llm']}，对话检索对 = {profile['pair_count']} 条")
    print(f"输出: {Path(args.profile_dir) / 'SKILL.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
