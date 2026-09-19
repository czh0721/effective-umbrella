import contextlib
import os
import tempfile
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, crypto, store, workspace  # noqa: E402
from ex_persona.config import PlatformConfig  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


class CreditStoreTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username):
        return store.create_user(username, "h", "s")

    def test_platform_defaults(self):
        config = PlatformConfig()
        self.assertFalse(config.ready)
        self.assertEqual(config.new_user_gift, 100)
        self.assertEqual(config.per_turn_cost, 1)

    def test_grant_summary(self):
        user = self._user("credit-grant")
        store.grant_credits(user["id"], 100, reason="gift", actor="system")
        self.assertEqual(store.get_credits(user["id"]), 100)
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["balance"], 100)
        self.assertEqual(summary["granted"], 100)
        self.assertEqual(summary["used"], 0)
        self.assertEqual(summary["ratio"], 0.0)
        self.assertEqual(summary["remaining_ratio"], 1.0)

    def test_deduct_ratio(self):
        user = self._user("credit-deduct")
        store.grant_credits(user["id"], 100)
        store.deduct_credits(user["id"], 30, reason="reply")
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["balance"], 70)
        self.assertEqual(summary["used"], 30)
        self.assertEqual(summary["granted"], 100)
        self.assertAlmostEqual(summary["ratio"], 0.3, places=3)
        self.assertAlmostEqual(summary["remaining_ratio"], 0.7, places=3)

    def test_deduct_insufficient_keeps_balance(self):
        user = self._user("credit-insufficient")
        store.grant_credits(user["id"], 5)
        with self.assertRaises(store.InsufficientCredits):
            store.deduct_credits(user["id"], 10)
        self.assertEqual(store.get_credits(user["id"]), 5)
        self.assertEqual(store.credits_summary(user["id"])["used"], 0)

    def test_ledger_balance_after(self):
        user = self._user("credit-ledger")
        store.grant_credits(user["id"], 40, reason="a", actor="admin:x")
        store.deduct_credits(user["id"], 10, reason="b")
        ledger = store.list_credit_ledger(user["id"], limit=10)
        self.assertEqual([item["delta"] for item in ledger], [-10, 40])
        self.assertEqual(ledger[0]["balance_after"], 30)

    def test_packages_seeded(self):
        packages = store.list_credit_packages(active_only=False)
        self.assertGreaterEqual(len(packages), 3)

    def test_upsert_package(self):
        package = store.upsert_credit_package(None, "测试包", 200, 100, "新", 50, True)
        self.assertEqual(package["credits"], 200)
        updated = store.upsert_credit_package(package["id"], "测试包", 300, 0, "", 50, False)
        self.assertEqual(updated["credits"], 300)
        self.assertEqual(updated["active"], 0)

    def test_upsert_package_missing_id_raises(self):
        with self.assertRaises(KeyError):
            store.upsert_credit_package(99999999, "幽灵包", 10, 0, "", 0, True)

    def test_summary_admin_clawback_reduces_remaining(self):
        user = self._user("credit-clawback")
        store.grant_credits(user["id"], 100, reason="gift")
        store.grant_credits(user["id"], -40, reason="管理员扣减", actor="admin:x")
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["balance"], 60)
        self.assertAlmostEqual(summary["remaining_ratio"], 0.6, places=3)

    def test_delete_user_soft_deletes_and_keeps_ledgers(self):
        user = self._user("credit-purge")
        store.grant_credits(user["id"], 30, reason="gift")
        store.grant_coins(user["id"], 20, reason="seed")
        store.delete_user(user["id"])
        # 软删：账号标记为 deleted，但账本与流水保留以便对账/恢复。
        self.assertEqual(store.get_user(user["id"])["status"], "deleted")
        self.assertEqual(len(store.list_credit_ledger(user["id"])), 1)
        self.assertEqual(len(store.list_coin_ledger(user["id"])), 1)

    def test_set_user_status_keeps_ledgers(self):
        user = self._user("status-keep")
        store.grant_credits(user["id"], 25, reason="gift")
        store.set_user_status(user["id"], "disabled")
        self.assertEqual(store.get_user(user["id"])["status"], "disabled")
        self.assertEqual(store.get_credits(user["id"]), 25)
        store.set_user_status(user["id"], "active")
        self.assertEqual(store.get_user(user["id"])["status"], "active")

    def test_purchase_package_idempotent(self):
        user = self._user("purchase-idem")
        store.grant_coins(user["id"], 1000)
        package = store.list_credit_packages(active_only=True)[0]

        first = store.purchase_package(user["id"], package["id"], idem="click-1")
        self.assertFalse(first["duplicate"])
        coins_after = store.get_coins(user["id"])
        credits_after = store.get_credits(user["id"])

        replay = store.purchase_package(user["id"], package["id"], idem="click-1")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(store.get_coins(user["id"]), coins_after)
        self.assertEqual(store.get_credits(user["id"]), credits_after)

        # 换一个幂等键表示新的一次购买，应正常扣费发放。
        again = store.purchase_package(user["id"], package["id"], idem="click-2")
        self.assertFalse(again["duplicate"])
        self.assertEqual(store.get_credits(user["id"]), credits_after + package["credits"])

    def test_grant_coins_and_credits_idempotent(self):
        user = self._user("grant-idem")
        first = store.grant_credits(user["id"], 50, reason="admin", idem="k1")
        self.assertFalse(first["duplicate"])
        replay = store.grant_credits(user["id"], 50, reason="admin", idem="k1")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(store.get_credits(user["id"]), 50)

        store.grant_coins(user["id"], 30, idem="c1")
        duplicate = store.grant_coins(user["id"], 30, idem="c1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(store.get_coins(user["id"]), 30)


class PersonaCleanupTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username):
        return store.create_user(username, "h", "s")

    def test_delete_persona_removes_children(self):
        user = self._user("cleanup-owner")
        persona = store.create_persona(user["id"], "清理", "")
        store.add_turn(user["id"], persona["id"], "user", "你好", contact="c1")
        store.add_memory(user["id"], persona["id"], "c1", "fact", "喜欢咖啡")
        store.touch_contact(user["id"], persona["id"], "c1")
        store.add_sticker(user["id"], persona["id"], "贴纸", "/tmp/sticker.png")

        store.delete_persona(user["id"], persona["id"])

        self.assertIsNone(store.get_persona(user["id"], persona["id"]))
        self.assertEqual(store.list_turns(user["id"], persona["id"]), [])
        self.assertEqual(store.count_memories(user["id"], persona["id"]), 0)
        self.assertEqual(store.list_contacts(user["id"], persona["id"]), [])
        self.assertEqual(store.list_stickers(user["id"], persona["id"]), [])

    def test_delete_memory_is_scoped_to_persona(self):
        user = self._user("memory-scope")
        first = store.create_persona(user["id"], "甲", "")
        second = store.create_persona(user["id"], "乙", "")
        store.add_memory(user["id"], first["id"], "c", "fact", "甲的记忆")
        store.add_memory(user["id"], second["id"], "c", "fact", "乙的记忆")
        memory = store.list_memories(user["id"], first["id"], "c")[0]

        self.assertFalse(store.delete_memory(user["id"], second["id"], memory["id"]))
        self.assertEqual(store.count_memories(user["id"], first["id"]), 1)

    def test_delete_sticker_is_scoped_to_persona(self):
        user = self._user("sticker-scope")
        first = store.create_persona(user["id"], "甲", "")
        second = store.create_persona(user["id"], "乙", "")
        sticker = store.add_sticker(user["id"], first["id"], "贴纸", "/tmp/s.png")

        self.assertIsNone(store.delete_sticker(user["id"], second["id"], sticker["id"]))
        self.assertEqual(len(store.list_stickers(user["id"], first["id"])), 1)
        self.assertIsNotNone(store.delete_sticker(user["id"], first["id"], sticker["id"]))


class CreditApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _admin(self, client, username):
        self._register(client, username)
        accounts.create_admin(username, "Password123!")
        login = client.post(
            "/api/admin/auth/login", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        return store.get_admin_by_username(username)

    def test_register_grants_initial_credits(self):
        with TestClient(app) as client:
            user = self._register(client, "credit-newbie")
            self.assertGreaterEqual(store.get_credits(user["id"]), 100)

    def test_credits_endpoint(self):
        with TestClient(app) as client:
            self._register(client, "credit-view")
            data = client.get("/api/credits").json()
            self.assertIn("balance", data)
            self.assertIn("per_turn_cost", data)
            self.assertGreaterEqual(len(data["packages"]), 3)
            self.assertIn("ledger", data)

    def test_me_includes_credits(self):
        with TestClient(app) as client:
            self._register(client, "credit-me")
            me = client.get("/api/me").json()
            self.assertIn("balance", me["credits"])
            self.assertIn("per_turn_cost", me["credits"])

    def test_me_includes_personal_stats(self):
        with TestClient(app) as client:
            user = self._register(client, "stats-me")
            me = client.get("/api/me").json()
            self.assertEqual(me["stats"]["persona_count"], 0)
            self.assertEqual(me["stats"]["memory_total"], 0)

            persona = store.create_persona(
                user["id"], "阿念", os.path.join(_TMP.name, "persona-stats")
            )
            store.add_memory(user["id"], persona["id"], "wx:test@im.wechat", "fact", "喜欢雨天")
            store.add_memory(user["id"], persona["id"], "wx:test@im.wechat", "fact", "常喝美式")

            me = client.get("/api/me").json()
            self.assertEqual(me["stats"]["persona_count"], 1)
            self.assertEqual(me["stats"]["memory_total"], 2)

    def test_admin_grant_and_deduct(self):
        with TestClient(app) as client:
            self._admin(client, "credit-admin-grant")
            target = store.create_user("credit-target", "h", "s")
            response = client.post(
                f"/api/admin/users/{target['id']}/credits",
                json={"delta": 500, "reason": "purchase"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["balance"], 500)
            response = client.post(
                f"/api/admin/users/{target['id']}/credits",
                json={"delta": -100, "reason": "refund"},
            )
            self.assertEqual(response.json()["balance"], 400)

    def test_admin_grant_rejects_overdraft(self):
        with TestClient(app) as client:
            self._admin(client, "credit-admin-overdraft")
            target = store.create_user("credit-poor", "h", "s")
            response = client.post(
                f"/api/admin/users/{target['id']}/credits", json={"delta": -1}
            )
            self.assertEqual(response.status_code, 400)

    def test_non_admin_cannot_grant(self):
        with TestClient(app) as client:
            self._register(client, "credit-plain")
            target = store.create_user("credit-plain-target", "h", "s")
            response = client.post(
                f"/api/admin/users/{target['id']}/credits", json={"delta": 10}
            )
            self.assertEqual(response.status_code, 401)

    def test_admin_users_include_credits(self):
        with TestClient(app) as client:
            self._admin(client, "credit-admin-users")
            response = client.get("/api/admin/users").json()
            self.assertTrue(all("credits" in item for item in response["items"]))

    def test_platform_config_update(self):
        with _isolated_db():
            with TestClient(app) as client:
                self._admin(client, "credit-admin-platform")
                response = client.put(
                    "/api/admin/platform",
                    json={
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-chat",
                        "api_key": "sk-test-platform-key",
                        "enabled": True,
                        "per_turn_cost": 2,
                        "new_user_gift": 50,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                data = client.get("/api/admin/platform").json()
                self.assertTrue(data["enabled"])
                self.assertTrue(data["has_key"])
                self.assertEqual(data["per_turn_cost"], 2)
                self.assertEqual(data["new_user_gift"], 50)
                self.assertNotIn("sk-test-platform-key", response.text)

    def test_platform_config_requires_admin(self):
        with _isolated_db():
            with TestClient(app) as client:
                self._register(client, "credit-plain-platform")
                self.assertEqual(client.get("/api/admin/platform").status_code, 401)
                self.assertEqual(
                    client.put("/api/admin/platform", json={"enabled": True}).status_code, 401
                )


class BillingPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _setup_chat(self, client, token, name, cost, gift):
        store.set_platform_config(
            crypto.encrypt("sk-platform"), "https://api.deepseek.com/v1",
            "deepseek-chat", True, cost, gift,
        )
        user = self._register(client, f"{name}-{token}")
        persona = client.post("/api/personas", json={"name": name}).json()["persona"]
        store.upsert_wechat_binding(
            user["id"], bridge_token=token,
            home_dir=str(workspace.home_dir(user["id"])), persona_id=persona["id"],
        )
        return user, persona

    def _patch_agent(self, reply, calls):
        from ex_persona import webapp as webapp_module

        class _Config:
            ready = True
            platform = True
            model = "deepseek-chat"
            base_url = ""

        class _Agent:
            config = _Config()
            last_error = None
            last_fallback = False

            def reply(self, *args, **kwargs):
                calls.append(1)
                return reply

            def build_system(self, *args, **kwargs):
                return ""

        original = webapp_module.get_agent
        webapp_module.get_agent = lambda uid, p: _Agent()
        return original

    def test_reply_deducts_per_turn_cost(self):
        with _isolated_db():
            with TestClient(app) as client:
                user, _ = self._setup_chat(client, "token-bill", "计费", 2, 100)
                start = store.get_credits(user["id"])
                calls = []
                from ex_persona import webapp as webapp_module

                original = self._patch_agent("你好", calls)
                try:
                    response = client.post(
                        "/v1/chat/completions/token-bill",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.get_credits(user["id"]), start - 2)

    def test_media_only_reply_charged_not_refunded(self):
        import json as _json

        from ex_persona import webapp as webapp_module

        with _isolated_db():
            with TestClient(app) as client:
                user, persona = self._setup_chat(client, "token-media-only", "表情", 2, 100)
                upload = client.post(
                    f"/api/personas/{persona['id']}/stickers",
                    files={"file": ("cute.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
                )
                self.assertEqual(upload.status_code, 200, upload.text)
                store.update_persona(
                    user["id"], persona["id"],
                    settings=_json.dumps({"advanced": {"reply_sticker": True}}),
                )
                start = store.get_credits(user["id"])
                calls: list = []
                queued: list = []
                original_queue = webapp_module._queue_media_replies
                restore_agent = self._patch_agent("[[STICKER:1]]", calls)
                webapp_module._queue_media_replies = (
                    lambda uid, p, c, s, d: (queued.append(d) or len(d))
                )
                try:
                    response = client.post(
                        "/v1/chat/completions/token-media-only",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = restore_agent
                    webapp_module._queue_media_replies = original_queue
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["choices"][0]["message"]["content"], "")
                self.assertTrue(queued, "纯表情回复也要把表情排进发送队列")
                self.assertEqual(
                    store.get_credits(user["id"]), start - 2,
                    "表情已经发出，属于成功回复，不能把本轮积分退回",
                )

    def test_reply_cache_not_charged_twice(self):
        with _isolated_db():
            with TestClient(app) as client:
                user, _ = self._setup_chat(client, "token-bill-cache", "缓存", 3, 100)
                start = store.get_credits(user["id"])
                calls = []
                from ex_persona import webapp as webapp_module

                original = self._patch_agent("在的", calls)
                try:
                    payload = {"user": "cache-contact", "messages": [{"role": "user", "content": "独一无二的消息"}]}
                    first = client.post("/v1/chat/completions/token-bill-cache", json=payload)
                    second = client.post("/v1/chat/completions/token-bill-cache", json=payload)
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.get_credits(user["id"]), start - 3)

    def test_zero_cost_is_free(self):
        with _isolated_db():
            with TestClient(app) as client:
                user, _ = self._setup_chat(client, "token-bill-free", "免费", 0, 100)
                start = store.get_credits(user["id"])
                calls = []
                from ex_persona import webapp as webapp_module

                original = self._patch_agent("免费回复", calls)
                try:
                    response = client.post(
                        "/v1/chat/completions/token-bill-free",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(store.get_credits(user["id"]), start)

    def test_fallback_reply_refunded_and_not_stored(self):
        from ex_persona import webapp as webapp_module

        class _Config:
            ready = True
            platform = True

        class _Agent:
            config = _Config()
            last_error = None
            last_fallback = True

            def reply(self, *args, **kwargs):
                return "哈哈，刚在忙，晚点回你。"

        with _isolated_db():
            with TestClient(app) as client:
                user, persona = self._setup_chat(client, "token-fallback", "兜底", 2, 100)
                start = store.get_credits(user["id"])
                original = webapp_module.get_agent
                webapp_module.get_agent = lambda uid, p: _Agent()
                try:
                    response = client.post(
                        "/v1/chat/completions/token-fallback",
                        json={"user": "fb-contact",
                              "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json()["choices"][0]["message"]["content"],
                    "哈哈，刚在忙，晚点回你。",
                )
                # 降级回复退回扣费，且不写入历史（否则模型会把兜底话术当范例复读）。
                self.assertEqual(store.get_credits(user["id"]), start)
                roles = [
                    turn["role"]
                    for turn in store.list_turns(user["id"], persona["id"], limit=10)
                ]
                self.assertEqual(roles.count("assistant"), 0)

    def test_empty_reply_refunded_and_not_stored(self):
        from ex_persona import webapp as webapp_module

        class _Config:
            ready = True
            platform = True

        class _Agent:
            config = _Config()
            last_error = None
            last_fallback = False

            def reply(self, *args, **kwargs):
                return ""

        with _isolated_db():
            with TestClient(app) as client:
                user, persona = self._setup_chat(client, "token-empty", "空回复", 2, 100)
                start = store.get_credits(user["id"])
                original = webapp_module.get_agent
                webapp_module.get_agent = lambda uid, p: _Agent()
                try:
                    response = client.post(
                        "/v1/chat/completions/token-empty",
                        json={"user": "empty-contact",
                              "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(response.status_code, 200, response.text)
                # 没产出可发送内容（乱码丢弃 / 静默）不算成功回复：退回积分、不写助手消息。
                self.assertEqual(response.json()["choices"][0]["message"]["content"], "")
                self.assertEqual(store.get_credits(user["id"]), start)
                roles = [
                    turn["role"]
                    for turn in store.list_turns(user["id"], persona["id"], limit=10)
                ]
                self.assertEqual(roles.count("assistant"), 0)

    def test_retired_persona_stops_replying(self):
        from ex_persona import webapp as webapp_module

        with _isolated_db():
            with TestClient(app) as client:
                user, persona = self._setup_chat(client, "token-retired", "告别", 2, 100)
                store.update_persona(user["id"], persona["id"], status="retired")
                calls = []
                original = self._patch_agent("不该出现", calls)
                try:
                    response = client.post(
                        "/v1/chat/completions/token-retired",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(calls, [])
                self.assertEqual(response.json()["choices"][0]["message"]["content"], "")

    def test_debug_counts_against_platform_quota(self):
        from ex_persona import webapp as webapp_module

        with _isolated_db():
            os.environ["PERSONA_PLATFORM_DAILY_LIMIT"] = "1"
            try:
                with TestClient(app) as client:
                    _, persona = self._setup_chat(client, "token-quota", "试聊", 1, 100)
                    calls = []
                    original = self._patch_agent("试聊回复", calls)
                    try:
                        first = client.post(
                            f"/api/personas/{persona['id']}/debug", json={"message": "你好"}
                        )
                        second = client.post(
                            f"/api/personas/{persona['id']}/debug", json={"message": "在吗"}
                        )
                    finally:
                        webapp_module.get_agent = original
                    self.assertEqual(len(calls), 1)
                    self.assertNotIn("额度", first.json().get("error", ""))
                    self.assertIn("额度", second.json().get("error", ""))
            finally:
                os.environ.pop("PERSONA_PLATFORM_DAILY_LIMIT", None)

    def test_insufficient_credits_skips_model(self):
        with _isolated_db():
            with TestClient(app) as client:
                user, _ = self._setup_chat(client, "token-bill-poor", "没钱", 10, 0)
                calls = []
                from ex_persona import webapp as webapp_module

                original = self._patch_agent("不该出现", calls)
                try:
                    response = client.post(
                        "/v1/chat/completions/token-bill-poor",
                        json={"user": "c1", "messages": [{"role": "user", "content": "在吗"}]},
                    )
                finally:
                    webapp_module.get_agent = original
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(calls), 0)
                self.assertIn("积分用完了", response.json()["choices"][0]["message"]["content"])
                self.assertEqual(store.get_credits(user["id"]), 0)


class CoinCurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def _admin(self, client, username):
        self._register(client, username)
        accounts.create_admin(username, "Password123!")
        login = client.post(
            "/api/admin/auth/login", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        return store.get_admin_by_username(username)

    def test_coins_default_and_grant(self):
        user = store.create_user("coin-grant", "h", "s")
        self.assertEqual(store.get_coins(user["id"]), 0)
        store.grant_coins(user["id"], 50, reason="seed", actor="system")
        self.assertEqual(store.get_coins(user["id"]), 50)
        summary = store.coins_summary(user["id"])
        self.assertEqual(summary["balance"], 50)
        self.assertEqual(summary["granted"], 50)
        ledger = store.list_coin_ledger(user["id"])
        self.assertEqual(ledger[0]["delta"], 50)

    def test_grant_coins_insufficient(self):
        user = store.create_user("coin-poor", "h", "s")
        with self.assertRaises(store.InsufficientCoins):
            store.grant_coins(user["id"], -1)
        self.assertEqual(store.get_coins(user["id"]), 0)

    def test_purchase_debits_coins_credits_quota(self):
        user = store.create_user("coin-buyer", "h", "s")
        store.grant_coins(user["id"], 100)
        package = store.upsert_credit_package(None, "币购包", 300, 0, "", 90, True, coins=40)
        result = store.purchase_package(user["id"], package["id"])
        self.assertEqual(result["spent"], 40)
        self.assertEqual(store.get_coins(user["id"]), 60)
        self.assertEqual(store.get_credits(user["id"]), 300)
        self.assertEqual(result["coins"], 60)
        self.assertEqual(result["credits"], 300)

    def test_purchase_insufficient_keeps_balance(self):
        user = store.create_user("coin-broke", "h", "s")
        store.grant_coins(user["id"], 10)
        package = store.upsert_credit_package(None, "昂贵包", 999, 0, "", 91, True, coins=99)
        with self.assertRaises(store.InsufficientCoins):
            store.purchase_package(user["id"], package["id"])
        self.assertEqual(store.get_coins(user["id"]), 10)
        self.assertEqual(store.get_credits(user["id"]), 0)

    def test_redemption_code_single_use(self):
        user = store.create_user("coin-redeem", "h", "s")
        codes = store.create_redemption_codes(2, 15, batch="b1", actor="admin:test")
        self.assertEqual(len(codes), 2)
        result = store.redeem_code(user["id"], codes[0]["code"].lower())
        self.assertEqual(result["amount"], 15)
        self.assertEqual(store.get_coins(user["id"]), 15)
        with self.assertRaises(store.RedemptionError):
            store.redeem_code(user["id"], codes[0]["code"])
        self.assertEqual(store.get_coins(user["id"]), 15)

    def test_redemption_invalid_code(self):
        user = store.create_user("coin-bad-code", "h", "s")
        with self.assertRaises(store.RedemptionError):
            store.redeem_code(user["id"], "NIAN-XXXX-XXXX-XXXX")

    def test_seeded_packages_have_coin_price(self):
        packages = store.list_credit_packages(active_only=False)
        self.assertTrue(all(int(item["coins"]) > 0 for item in packages))

    def test_api_credits_includes_coins(self):
        with _isolated_db():
            with TestClient(app) as client:
                self._register(client, "coin-api-view")
                data = client.get("/api/credits").json()
                self.assertIn("coins", data)
                self.assertIn("coins_summary", data)
                self.assertIn("remaining_ratio", data["summary"])
                self.assertIn("coin_ledger", data)
                me = client.get("/api/me").json()
                self.assertIn("coins", me["credits"])

    def test_api_redeem_and_purchase(self):
        with _isolated_db():
            with TestClient(app) as client:
                self._admin(client, "coin-api-admin")
                created = client.post(
                    "/api/admin/redemption-codes", json={"count": 1, "coins": 80}
                ).json()
                self.assertEqual(created["stats"]["unused"], 1)
                code = created["items"][0]["code"]

                client.post("/api/auth/logout")
                self._register(client, "coin-api-user")
                redeem = client.post("/api/redeem", json={"code": code.lower()})
                self.assertEqual(redeem.status_code, 200, redeem.text)
                self.assertEqual(redeem.json()["coins"], 80)
                self.assertEqual(
                    client.post("/api/redeem", json={"code": code}).status_code, 400
                )

                package = client.get("/api/credits").json()["packages"][0]
                cost = int(package["coins"])
                start_credits = client.get("/api/credits").json()["balance"]
                store.grant_coins(store.get_user_by_username("coin-api-user")["id"], 1000)
                purchase = client.post(f"/api/packages/{package['id']}/purchase")
                self.assertEqual(purchase.status_code, 200, purchase.text)
                self.assertEqual(purchase.json()["credits"], start_credits + int(package["credits"]))
                self.assertEqual(purchase.json()["coins"], 1080 - cost)

    def test_admin_codes_require_admin(self):
        with TestClient(app) as client:
            self._register(client, "coin-api-plain")
            self.assertEqual(
                client.post("/api/admin/redemption-codes", json={"count": 1, "coins": 5}).status_code,
                401,
            )
            self.assertEqual(client.get("/api/admin/redemption-codes").status_code, 401)

    def test_admin_users_include_coins(self):
        with TestClient(app) as client:
            self._admin(client, "coin-api-users")
            response = client.get("/api/admin/users").json()
            self.assertTrue(all("coins" in item for item in response["items"]))


@contextlib.contextmanager
def _isolated_db():
    old = os.environ.get("PERSONA_DB_PATH")
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    os.environ["PERSONA_DB_PATH"] = handle.name
    try:
        store.init_db()
        yield
    finally:
        if old is None:
            os.environ.pop("PERSONA_DB_PATH", None)
        else:
            os.environ["PERSONA_DB_PATH"] = old
