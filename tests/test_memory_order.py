"""长期记忆展示顺序回归测试。

用户在「长期记忆」里看到的是按时间正序排列的卡片：最新的日期反而在最下面。
记忆列表接口应该把最新的一条放在最前面；而对话、朋友圈等需要按时间线理解的
场景仍然要按正序拿到记忆，不能被这次改动带偏。
"""

import os
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import store  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


class MemoryOrderTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_store_default_is_chronological(self):
        user = store.create_user("mem-order-store", "h", "s")
        persona = store.create_persona(user["id"], "记忆", "")
        store.add_memory(user["id"], persona["id"], "c1", "fact", "第一条")
        store.add_memory(user["id"], persona["id"], "c1", "fact", "第二条")
        store.add_memory(user["id"], persona["id"], "c1", "fact", "第三条")

        chronological = store.list_memories(user["id"], persona["id"], "c1")
        self.assertEqual([m["content"] for m in chronological], ["第一条", "第二条", "第三条"])

        newest_first = store.list_memories(user["id"], persona["id"], "c1", order="desc")
        self.assertEqual([m["content"] for m in newest_first], ["第三条", "第二条", "第一条"])

    def test_memories_api_returns_newest_first(self):
        with TestClient(app) as client:
            user = self._register(client, "mem-order-api")
            persona = client.post("/api/personas", json={"name": "记忆"}).json()["persona"]
            store.add_memory(user["id"], persona["id"], "c1", "fact", "第一条")
            store.add_memory(user["id"], persona["id"], "c1", "fact", "第二条")
            store.add_memory(user["id"], persona["id"], "c1", "fact", "第三条")

            response = client.get(f"/api/personas/{persona['id']}/memories", params={"contact": "c1"})
            self.assertEqual(response.status_code, 200, response.text)
            contents = [m["content"] for m in response.json()["memories"]]
            self.assertEqual(contents, ["第三条", "第二条", "第一条"])


if __name__ == "__main__":
    unittest.main()
