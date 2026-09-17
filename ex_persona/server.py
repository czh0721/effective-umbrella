import os
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .agent import PersonaAgent

app = FastAPI(title="ex-persona", version="0.1.0")
_agent: PersonaAgent | None = None


def get_agent() -> PersonaAgent:
    global _agent
    if _agent is None:
        profile_dir = os.getenv("PERSONA_PROFILE_DIR", "data/profile")
        _agent = PersonaAgent(profile_dir)
    return _agent


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "profile": os.getenv("PERSONA_PROFILE_DIR", "data/profile")}


@app.get("/v1/models")
def models() -> dict:
    agent = get_agent()
    return {
        "object": "list",
        "data": [{"id": agent.config.model, "object": "model", "owned_by": "ex-persona"}],
    }


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest) -> dict:
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    last_user = None
    for message in reversed(request.messages):
        if message.role == "user":
            last_user = message.content
            break
    if last_user is None:
        raise HTTPException(status_code=400, detail="缺少 user 消息")

    history = [
        {"role": message.role, "content": message.content}
        for message in request.messages
        if message.role in ("user", "assistant")
    ]
    if history and history[-1]["content"] == last_user:
        history = history[:-1]

    agent = get_agent()
    try:
        reply = agent.reply(
            last_user,
            history=history,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model or agent.config.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
