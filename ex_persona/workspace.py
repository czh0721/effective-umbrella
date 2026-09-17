"""用户工作区：按 user_id 隔离的目录布局与路径安全校验。"""

import os
import re
import time
import uuid
from pathlib import Path

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff-]+")


def data_root() -> Path:
    return Path(os.getenv("PERSONA_DATA_DIR", "data")).expanduser().resolve()


def users_root() -> Path:
    return data_root() / "users"


def user_root(user_id: int) -> Path:
    return users_root() / str(int(user_id))


def raw_dir(user_id: int) -> Path:
    return user_root(user_id) / "raw"


def personas_root(user_id: int) -> Path:
    return user_root(user_id) / "profile"


def persona_dir(user_id: int, persona_id: int) -> Path:
    return personas_root(user_id) / str(int(persona_id))


def home_dir(user_id: int) -> Path:
    return user_root(user_id) / "home"


def ensure_user(user_id: int) -> Path:
    root = user_root(user_id)
    for path in (root, raw_dir(user_id), personas_root(user_id), home_dir(user_id)):
        path.mkdir(parents=True, exist_ok=True)
    return root


def ensure_persona(user_id: int, persona_id: int) -> Path:
    path = persona_dir(user_id, persona_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str) -> str:
    """把上传文件名规整为安全的裸文件名。"""
    base = Path(name or "chat.txt").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "chat.txt"
    return cleaned[:120]


def safe_join(root: Path, name: str) -> Path:
    """在 root 下拼接文件名，拒绝任何越界路径。"""
    candidate = (root / safe_filename(name)).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise ValueError("非法的文件路径")
    return candidate


def new_batch(user_id: int, kind: str) -> Path:
    batch = raw_dir(user_id) / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    batch.mkdir(parents=True, exist_ok=True)
    return batch
