"""本地假模型服务：OpenAI 兼容 /v1/chat/completions，用于全链路回归。

按请求内容分流：
- 蒸馏分析师系统提示 -> 返回人格画像 JSON
- 记忆抽取器系统提示 -> 返回一条记忆 JSON
- JSON 修复器 -> 返回合法 JSON（兜底）
- 其余（角色回复）-> 按最后一条 user 消息里的触发词返回长回复 / 空 / 乱码
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LONG_REPLY = (
    "刚忙完 正想给你发消息呢。你今天怎么突然想起我啦 是不是遇到什么事了 跟我说说呗。"
    "我最近也挺想你的 前两天路过那家我们常去的咖啡店 还想起你总爱点冰美式加两份糖。"
    "你说太苦喝不下去 现在还是这样吗 要是还这样的话 下次我给你带一杯 保证不苦。"
    "对了 你上次说的那个工作上的事 后来怎么样了 别自己一个人扛着 跟我说说。"
    "今天下午我这边下了一场小雨 路上湿漉漉的 我撑着伞走回家 突然就想起以前我们一起淋雨的样子。"
    "那时候你总说不怕感冒 结果第二天还是发烧了 我跑了好几家药店才买到退烧药。"
    "现在想想还挺好笑的 我们那时候真是又傻又开心 你要是得空就回我一句 我一直都在。"
    "晚上记得吃点热的 别总是随便对付一口 胃不好就少喝点冰的 听我一句劝。"
)

CARD = {
    "summary": "一个嘴硬心软、爱操心的老朋友。",
    "personality": ["细心", "念旧", "爱操心"],
    "speaking_style": ["短句为主", "口语化", "会反问"],
    "emotional_patterns": ["表面调侃，实则关心"],
    "values_and_attitudes": ["重视陪伴"],
    "relationship_with_me": ["多年好友，彼此照应"],
    "favorite_phrases": ["跟我说说呗", "别自己扛着"],
    "topics": ["咖啡", "工作", "日常"],
    "boundaries": ["不喜欢被追问隐私"],
    "identity": {"name": "小鹿", "gender": "女", "birthday": "", "age": "", "hometown": "", "education": ""},
    "user": {"relationship": "老朋友", "met_time": "大学时代"},
    "soul": {"speaking_style": ["短句", "口语"], "catchphrases": ["跟我说说呗"]},
    "memories": [
        {"title": "常去的咖啡店", "detail": "总点冰美式加两份糖", "time": "大学时代"},
        {"title": "一起熬夜赶工", "detail": "互相打气到天亮", "time": "毕业那年"},
    ],
}

MEMORY = {"memories": [{"kind": "fact", "content": "对方喜欢喝冰美式加两份糖"}]}
GARBLE = "正常开头的文字" + ("\ue000\ue001\ue002\ue003" * 5)


def _content_for(messages):
    system = " ".join(m.get("content") or "" for m in messages if m.get("role") == "system")
    last_user = ""
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            last_user = msg["content"]
    if "人格蒸馏分析师" in system:
        return json.dumps(CARD, ensure_ascii=False)
    if "对话记忆抽取器" in system:
        return json.dumps(MEMORY, ensure_ascii=False)
    if "JSON 修复器" in system:
        return json.dumps(CARD, ensure_ascii=False)
    if "触发空回复" in last_user or "[[SILENCE]]" in last_user:
        return ""
    if "触发乱码" in last_user:
        return GARBLE
    return LONG_REPLY


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - 静默
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        content = _content_for(payload.get("messages") or [])
        body = json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": payload.get("model") or "fake-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(port=8791):
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
