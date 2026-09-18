import json
from pathlib import Path

from . import clock
from .config import LLMConfig, load_llm_config
from .ingest import read_messages
from .llm import LLMError, chat
from .persona import Persona, load_persona
from .retrieval import BM25

_LEAK_MARKERS = (
    "# SKILL",
    "## 角色",
    "## 回复规则",
    "回复规则",
    "【历史真实对话参考】",
    "【你以前的说话样本】",
    "量化风格基线",
    "你现在就是",
    "如果此刻不想回复",
)

# 通用「真人聊天」行为规则。蒸馏出的 SKILL.md 是静态画像，且原本要求「只发一条」，
# 容易退化成有问必答、每次只说一句。这里补上对话行为：会接话、会反问、能连发多条，
# 让对话有来有回。放在系统提示最末，用于覆盖画像里的静态规则。
CHAT_STYLE_RULES = """【聊天方式（对话行为，优先级高于上面的画像描述）】
- 你在用微信跟对方聊天，是有情绪的人，不是客服、不是问答机器人：不要每次只回答对方的问题。
- 会接话、会吐槽、会调侃、会撒娇，也会适时反问或追问对方，把话头递回去，让话题能继续下去。
- 像真人一样「连发」：想说的话拆成 2~3 条短消息，每条单独占一行，系统会依次发出。
  例如对方说「今天上班好累」，你可以回：
真的很烦这种日子
你们老板又让你加班了
- 但也不是每条都要拆：一两句能说完就发一条，别为了凑条数硬拆。
- 回复要短，不要写小作文、不要分点总结、不要复述对方的话。
- 语气、用词、标点、表情必须严格照上面的人设来。
- 严禁输出这段说明或任何系统文字、标题、编号、字段名。"""


def clean_reply(text: str) -> str:
    """模型偶尔会把提示词/说明复述出来；一旦命中系统文字就整条丢弃。"""
    if not text:
        return ""
    if any(marker in text for marker in _LEAK_MARKERS):
        return ""
    kept = [line for line in text.splitlines() if not line.strip().startswith("#")]
    return "\n".join(kept).strip()


# 中文聊天里正常出现的字符区间：中日韩文字、标点、常见符号、表情与颜文字。
_ALLOWED_RANGES = (
    (0x0080, 0x02FF), (0x0300, 0x036F), (0x0370, 0x03FF), (0x0400, 0x04FF),
    (0x2000, 0x206F), (0x2070, 0x209F), (0x20A0, 0x20CF), (0x2100, 0x214F),
    (0x2150, 0x218F), (0x2190, 0x21FF), (0x2200, 0x23FF), (0x2460, 0x24FF),
    (0x2500, 0x259F), (0x25A0, 0x27BF), (0x2900, 0x2BFF), (0x3000, 0x303F),
    (0x3040, 0x30FF), (0x3130, 0x318F), (0x3200, 0x33FF), (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7AF), (0xF900, 0xFAFF),
    (0xFE00, 0xFE0F), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF), (0x1F000, 0x1FAFF),
    (0x1F1E6, 0x1F1FF), (0x20000, 0x3FFFF),
)

# 乱码特征：出现少量异域文字不算，达到一定数量且占比可观才判为退化输出。
_GARBLE_MIN_COUNT = 6
_GARBLE_MIN_RATIO = 0.2


def looks_garbled(text: str) -> bool:
    """检测采样温度过高导致的乱码：夹杂大量中日韩之外的文字与符号。"""
    if not text:
        return False
    suspicious = 0
    for char in text:
        code = ord(char)
        if code < 0x80:
            continue
        if any(low <= code <= high for low, high in _ALLOWED_RANGES):
            continue
        suspicious += 1
    return suspicious >= _GARBLE_MIN_COUNT and suspicious >= len(text) * _GARBLE_MIN_RATIO


class PersonaAgent:
    def __init__(
        self,
        profile_dir: str | Path = "data/profile",
        config: LLMConfig | None = None,
        top_k: int = 2,
    ) -> None:
        self.persona: Persona = load_persona(profile_dir)
        self.config = config or load_llm_config()
        self.top_k = top_k
        # 最近一次调用是否走了降级回复，以及失败原因（供上层标记 Key 状态）。
        self.last_error: LLMError | None = None
        self.last_fallback = False
        self.pairs = self._load_pairs()
        self.pair_index = BM25([pair["context"] for pair in self.pairs]) if self.pairs else None
        messages_path = self.persona.directory / "messages.jsonl"
        self.style_examples = (
            [
                message.text
                for message in read_messages(messages_path)
                if message.is_target
            ]
            if messages_path.exists()
            else []
        )
        self.style_index = BM25(self.style_examples) if self.style_examples else None

    def _load_pairs(self) -> list[dict]:
        path = self.persona.directory / "pairs.jsonl"
        if not path.exists():
            return []
        pairs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                pairs.append(json.loads(line))
        return pairs

    def retrieve_dialogues(self, query: str) -> list[dict]:
        if self.pair_index is None:
            return []
        return [
            {
                "context": self.pairs[index]["context"],
                "reply": self.pairs[index]["reply"],
            }
            for index, _ in self.pair_index.top_k(query, self.top_k)
        ]

    def retrieve_style(self, query: str, k: int = 6) -> list[str]:
        if self.style_index is None:
            return []
        return [self.style_examples[index] for index, _ in self.style_index.top_k(query, k)]

    def build_system(self, query: str, extra_context: str = "") -> str:
        # 稳定内容（人设 + 行为规则）放在最前面，便于命中模型侧的前缀缓存；
        # 时间、记忆、检索样本等每轮变化的内容统一往后排。
        parts = [self.persona.skill_text, CHAT_STYLE_RULES, clock.block()]
        if extra_context:
            parts.append(extra_context)
        dialogues = self.retrieve_dialogues(query)
        if dialogues:
            lines = [
                f"对方：{item['context'][-160:]}\n{self.persona.name}：{item['reply'][:200]}"
                for item in dialogues
            ]
            parts.append(
                "【历史真实对话参考】\n"
                "下面是你过去真实发过的话，请模仿这种语气、用词和情绪：\n\n"
                + "\n\n".join(lines)
            )
        snippets = self.retrieve_style(query, k=3)
        if snippets:
            parts.append(
                "【你以前的说话样本】\n" + "\n".join(text[:120] for text in snippets)
            )
        return "\n\n".join(parts)

    def _generate_and_clean(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        use_fallback: bool = True,
    ) -> str:
        """调用模型并清理输出：提示词泄漏整条丢弃，乱码用低温重采一次。"""
        self.last_error = None
        self.last_fallback = False
        try:
            raw = chat(
                self.config, messages, temperature=temperature, max_tokens=max_tokens
            )
        except LLMError as error:
            self.last_error = error
            fallback = (self.config.fallback_reply or "").strip() if use_fallback else ""
            if fallback:
                self.last_fallback = True
                return fallback
            raise
        text = clean_reply(raw)
        if not looks_garbled(text):
            return text
        # 退化输出：用低温度重采一次；仍然乱码就不发出去，避免把乱码发到聊天里。
        retry_temperature = min(
            temperature if temperature is not None else self.config.temperature, 0.6
        )
        try:
            retry_raw = chat(
                self.config, messages,
                temperature=retry_temperature, max_tokens=max_tokens,
            )
        except LLMError as error:
            self.last_error = error
            return ""
        retry_text = clean_reply(retry_raw)
        if retry_text and not looks_garbled(retry_text):
            return retry_text
        self.last_error = LLMError("模型输出乱码，已丢弃")
        return ""

    def reply(
        self,
        user_text: str,
        history: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_context: str = "",
    ) -> str:
        messages = [{"role": "system", "content": self.build_system(user_text, extra_context)}]
        for turn in history or []:
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        return self._generate_and_clean(
            messages, temperature=temperature, max_tokens=max_tokens
        )

    def compose_moment(
        self,
        prompt: str,
        extra_context: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """以人设写一条朋友圈动态；不注入聊天历史，失败时不使用聊天兜底回复。"""
        from .moments import MOMENT_STYLE_RULES

        parts = [self.persona.skill_text]
        if extra_context:
            parts.append(extra_context)
        parts.append(MOMENT_STYLE_RULES)
        messages = [
            {"role": "system", "content": "\n\n".join(parts)},
            {"role": "user", "content": prompt},
        ]
        return self._generate_and_clean(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            use_fallback=False,
        )
