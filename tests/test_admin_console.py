import os
import tempfile
import unittest
import uuid
from datetime import date, timedelta
from unittest import mock

if "PERSONA_DATA_DIR" not in os.environ:
    _TMP = tempfile.TemporaryDirectory()
    os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ.setdefault("PERSONA_SECRET_KEY", "unit-test-secret")
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, store  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _make_admin(username, password="password123"):
    return accounts.create_admin(username, password)


def _admin_login(client, username, password="password123"):
    response = client.post(
        "/api/admin/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _register(client, username):
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "password123"}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


class DashboardTest(unittest.TestCase):
    def test_dashboard_payload_and_trend(self):
        with TestClient(app) as client:
            store.invalidate_dashboard()
            username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(username)
            _admin_login(client, username)
            response = client.get("/api/admin/dashboard?days=7")
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertIn("kpis", data)
            self.assertIn("funnel", data)
            self.assertEqual(len(data["trend"]), 7)
            for key in ("total_users", "active_today", "total_personas", "turns_today"):
                self.assertIn(key, data["kpis"])
            for key in ("registered", "with_persona", "with_conversation", "paying"):
                self.assertIn(key, data["funnel"])
            # 日期轴连续
            dates = [date.fromisoformat(item["date"]) for item in data["trend"]]
            for earlier, later in zip(dates, dates[1:], strict=False):
                self.assertEqual(later - earlier, timedelta(days=1))

    def test_dashboard_cache_hit(self):
        with mock.patch.dict(os.environ, {"PERSONA_DASHBOARD_CACHE_TTL": "60"}):
            store.invalidate_dashboard()
            first = store.admin_dashboard(7)
            second = store.admin_dashboard(7)
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            store.invalidate_dashboard()
            third = store.admin_dashboard(7)
            self.assertFalse(third["cached"])

    def test_dashboard_requires_admin(self):
        with TestClient(app) as client:
            blocked = client.get("/api/admin/dashboard")
            self.assertEqual(blocked.status_code, 401)


class AdminUserManagementTest(unittest.TestCase):
    def test_search_filter_and_detail(self):
        with TestClient(app) as client:
            suffix = uuid.uuid4().hex[:8]
            target = _register(client, f"searchable-{suffix}")
            _register(client, f"other-{uuid.uuid4().hex[:8]}")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            found = client.get(f"/api/admin/users?q=searchable-{suffix}")
            self.assertEqual(found.status_code, 200, found.text)
            payload = found.json()
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], target["id"])

            detail = client.get(f"/api/admin/users/{target['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            user = detail.json()["user"]
            self.assertEqual(user["id"], target["id"])
            self.assertNotIn("password_hash", user)
            self.assertIn("ledger", user)

            saved = client.post(
                f"/api/admin/users/{target['id']}/note",
                json={"note": "重点跟进", "tags": "VIP,风险"},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            refreshed = client.get(f"/api/admin/users/{target['id']}").json()["user"]
            self.assertEqual(refreshed["note"], "重点跟进")
            self.assertEqual(refreshed["tags"], "VIP,风险")

    def test_disable_records_reason_and_kills_session(self):
        with TestClient(app) as client:
            user = _register(client, f"disable-{uuid.uuid4().hex[:8]}")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            disabled = client.post(
                f"/api/admin/users/{user['id']}/status",
                json={"status": "disabled", "reason": "违规内容"},
            )
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertEqual(store.get_user(user["id"])["status"], "disabled")
            detail = client.get(f"/api/admin/users/{user['id']}").json()["user"]
            self.assertEqual(detail["status_reason"], "违规内容")
            self.assertTrue(detail["disabled_at"])
            # 会话被注销
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_restore_clears_reason(self):
        with TestClient(app) as client:
            user = _register(client, f"restore-{uuid.uuid4().hex[:8]}")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)
            client.post(
                f"/api/admin/users/{user['id']}/status",
                json={"status": "disabled", "reason": "临时"},
            )
            restored = client.post(
                f"/api/admin/users/{user['id']}/status", json={"status": "active"}
            )
            self.assertEqual(restored.status_code, 200, restored.text)
            detail = client.get(f"/api/admin/users/{user['id']}").json()["user"]
            self.assertEqual(detail["status"], "active")
            self.assertEqual(detail["status_reason"], "")
            self.assertEqual(detail["disabled_at"], "")


class AdminOrderTest(unittest.TestCase):
    def _package(self):
        packages = store.list_credit_packages(active_only=True)
        self.assertTrue(packages)
        return packages[0]

    def test_purchase_records_order_and_revenue(self):
        with TestClient(app) as client:
            user = _register(client, f"buyer-{uuid.uuid4().hex[:8]}")
            store.grant_coins(user["id"], 1000, reason="测试发放")
            package = self._package()
            purchased = client.post(f"/api/packages/{package['id']}/purchase")
            self.assertEqual(purchased.status_code, 200, purchased.text)

            orders = [item for item in store.list_orders() if item["user_id"] == user["id"]]
            self.assertEqual(len(orders), 1)
            order = orders[0]
            self.assertEqual(order["user_id"], user["id"])
            self.assertEqual(order["package_id"], package["id"])
            self.assertEqual(order["coins"], package["coins"])
            self.assertEqual(
                order["credits"] + order["bonus_credits"], package["credits"] + package["bonus_credits"]
            )

            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)
            listing = client.get("/api/admin/orders")
            self.assertEqual(listing.status_code, 200, listing.text)
            body = listing.json()
            self.assertTrue(any(item["user_id"] == user["id"] for item in body["items"]))
            self.assertGreaterEqual(body["revenue"]["coins"], package["coins"])
            revenue = client.get("/api/admin/orders/revenue?days=7")
            self.assertEqual(revenue.status_code, 200, revenue.text)
            self.assertEqual(len(revenue.json()["trend"]), 7)

    def test_idempotent_purchase_does_not_duplicate_order(self):
        with TestClient(app) as client:
            user = _register(client, f"idem-{uuid.uuid4().hex[:8]}")
            store.grant_coins(user["id"], 1000, reason="测试发放")
            package = self._package()
            headers = {"Idempotency-Key": "order-idem-1"}
            first = client.post(f"/api/packages/{package['id']}/purchase", headers=headers)
            self.assertEqual(first.status_code, 200, first.text)
            second = client.post(f"/api/packages/{package['id']}/purchase", headers=headers)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertTrue(second.json()["duplicate"])
            self.assertEqual(
                len([item for item in store.list_orders() if item["user_id"] == user["id"]]), 1
            )


class AdminContentModerationTest(unittest.TestCase):
    def _persona(self, client, username):
        user = _register(client, username)
        created = client.post(
            "/api/personas", json={"name": "念念", "target_name": "前任"}
        )
        assert created.status_code == 200, created.text
        persona = store.get_active_persona(user["id"])
        self.assertIsNotNone(persona)
        return user, persona

    def test_persona_disable_and_restore(self):
        with TestClient(app) as client:
            user, persona = self._persona(client, f"moderator-{uuid.uuid4().hex[:8]}")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            listing = client.get("/api/admin/content/personas?limit=200")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertTrue(any(item["id"] == persona["id"] for item in listing.json()["items"]))

            stopped = client.post(
                f"/api/admin/content/personas/{persona['id']}/status",
                json={"status": "disabled", "reason": "违规"},
            )
            self.assertEqual(stopped.status_code, 200, stopped.text)
            self.assertEqual(store.get_persona(user["id"], persona["id"])["status"], "disabled")
            self.assertEqual(store.get_persona(user["id"], persona["id"])["is_active"], 0)

            detail = client.get(f"/api/admin/content/personas/{persona['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertIn("counts", detail.json()["persona"])

            restored = client.post(
                f"/api/admin/content/personas/{persona['id']}/status",
                json={"status": "ready"},
            )
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(store.get_persona(user["id"], persona["id"])["status"], "ready")

    def test_hidden_moment_is_filtered_from_user_feed(self):
        with TestClient(app) as client:
            user, persona = self._persona(client, f"moment-{uuid.uuid4().hex[:8]}")
            moment = store.add_moment(user["id"], persona["id"], "今天很想你", source="manual")
            self.assertEqual(
                len(store.list_moments(user["id"], persona["id"])), 1
            )
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            listing = client.get("/api/admin/content/moments?limit=200")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertTrue(any(item["id"] == moment["id"] for item in listing.json()["items"]))

            hidden = client.post(
                f"/api/admin/content/moments/{moment['id']}/hidden", json={"hidden": True}
            )
            self.assertEqual(hidden.status_code, 200, hidden.text)
            self.assertEqual(store.list_moments(user["id"], persona["id"]), [])

            visible = client.post(
                f"/api/admin/content/moments/{moment['id']}/hidden", json={"hidden": False}
            )
            self.assertEqual(visible.status_code, 200, visible.text)
            self.assertEqual(len(store.list_moments(user["id"], persona["id"])), 1)


class AdminOpsTest(unittest.TestCase):
    def test_announcement_lifecycle_and_notice(self):
        with TestClient(app) as client:
            user = _register(client, f"ops-{uuid.uuid4().hex[:8]}")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            created = client.post(
                "/api/admin/announcements",
                json={"title": "维护通知", "body": "今晚升级", "audience": "all"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            announcement_id = created.json()["announcement"]["id"]

            active = client.get("/api/announcements")
            self.assertEqual(active.status_code, 200, active.text)
            self.assertTrue(any(item["id"] == announcement_id for item in active.json()["items"]))

            off = client.post(
                f"/api/admin/announcements/{announcement_id}/active", json={"active": False}
            )
            self.assertEqual(off.status_code, 200, off.text)
            self.assertFalse(
                any(item["id"] == announcement_id for item in client.get("/api/announcements").json()["items"])
            )

            sent = client.post("/api/admin/notices", json={"message": "欢迎回来", "user_ids": [user["id"]]})
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertEqual(sent.json()["sent"], 1)
            self.assertGreaterEqual(store.count_unread_alerts(user["id"]), 1)

    def test_broadcast_notice_reaches_all_active_users(self):
        with TestClient(app) as client:
            user = _register(client, f"broadcast-{uuid.uuid4().hex[:8]}")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)
            sent = client.post("/api/admin/notices", json={"message": "全体通知", "user_ids": None})
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertGreaterEqual(sent.json()["sent"], 1)
            self.assertGreaterEqual(store.count_unread_alerts(user["id"]), 1)


class AdminSystemTest(unittest.TestCase):
    def test_feature_flags_roundtrip(self):
        with TestClient(app) as client:
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            flags = client.get("/api/admin/system/flags")
            self.assertEqual(flags.status_code, 200, flags.text)
            self.assertIn("moments_auto", flags.json()["flags"])

            updated = client.put("/api/admin/system/flags/moments_auto", json={"value": True})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(store.get_feature_flags()["moments_auto"], 1)

            blocked = client.put("/api/admin/system/flags/unknown_flag", json={"value": True})
            self.assertEqual(blocked.status_code, 404)

    def test_flags_default_on_and_immediate_effect(self):
        flags = store.get_feature_flags()
        for key in ("moments_auto", "wechat_login", "platform_model"):
            self.assertEqual(flags[key], 1)
            self.assertTrue(store.feature_flag_enabled(key))
        store.set_feature_flag("moments_auto", 0, "tester")
        self.assertFalse(store.feature_flag_enabled("moments_auto"))
        store.set_feature_flag("moments_auto", 1, "tester")

    def test_task_retry_and_backups_and_health(self):
        with TestClient(app) as client:
            user = _register(client, f"tasker-{uuid.uuid4().hex[:8]}")
            task = store.create_task(f"task-{uuid.uuid4().hex[:8]}", user["id"], None, "distill")
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            _admin_login(client, admin_username)

            listing = client.get("/api/admin/system/tasks?limit=200")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertTrue(any(item["id"] == task["id"] for item in listing.json()["items"]))

            retried = client.post(f"/api/admin/system/tasks/{task['id']}/retry")
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(store.get_task(task["id"])["status"], "pending")

            backups = client.get("/api/admin/system/backups")
            self.assertEqual(backups.status_code, 200, backups.text)
            self.assertIsInstance(backups.json()["items"], list)

            health = client.get("/api/admin/system/health")
            self.assertEqual(health.status_code, 200, health.text)
            self.assertEqual(health.json()["status"], "ok")
            self.assertIn("outbox", health.json())


class AdminManagementTest(unittest.TestCase):
    def test_create_status_and_scoped_actions(self):
        with TestClient(app) as client:
            admin_username = f"admin-{uuid.uuid4().hex[:8]}"
            _make_admin(admin_username)
            me = _admin_login(client, admin_username)

            listing = client.get("/api/admin/admins")
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertTrue(any(item["username"] == admin_username for item in listing.json()["items"]))

            created = client.post(
                "/api/admin/admins",
                json={"username": f"new-admin-{uuid.uuid4().hex[:8]}", "password": "password123", "name": "副手"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            new_id = created.json()["admin"]["id"]

            reset = client.post(
                f"/api/admin/admins/{new_id}/password", json={"password": "password456"}
            )
            self.assertEqual(reset.status_code, 200, reset.text)
            self.assertTrue(accounts.authenticate_admin(store.get_admin(new_id)["username"], "password456"))

            totp = client.post(f"/api/admin/admins/{new_id}/totp")
            self.assertEqual(totp.status_code, 200, totp.text)

            disabled = client.post(f"/api/admin/admins/{new_id}/status", json={"status": "disabled"})
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertEqual(store.get_admin(new_id)["status"], "disabled")

            self_reset = client.post(
                f"/api/admin/admins/{me['admin']['id']}/status", json={"status": "disabled"}
            )
            self.assertEqual(self_reset.status_code, 400)


class AdminExportTest(unittest.TestCase):
    def _admin_client(self, client):
        username = f"admin-{uuid.uuid4().hex[:8]}"
        _make_admin(username)
        _admin_login(client, username)
        return client

    def test_exports_are_utf8_bom_csv(self):
        with TestClient(app) as client:
            _register(client, f"exportee-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)

            users = client.get("/api/admin/users/export")
            self.assertEqual(users.status_code, 200, users.text)
            self.assertIn("text/csv", users.headers["content-type"])
            self.assertIn("user", users.headers["content-disposition"])
            self.assertTrue(users.content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("用户名", users.content.decode("utf-8"))

            audit = client.get("/api/admin/audit/export")
            self.assertEqual(audit.status_code, 200, audit.text)
            self.assertTrue(audit.content.startswith(b"\xef\xbb\xbf"))

            orders = client.get("/api/admin/orders/export")
            self.assertEqual(orders.status_code, 200, orders.text)
            self.assertTrue(orders.content.startswith(b"\xef\xbb\xbf"))

    def test_audit_filters_and_export_requires_admin(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/admin/users/export").status_code, 401)
            self.assertEqual(client.get("/api/admin/orders/export").status_code, 401)
            self.assertEqual(client.get("/api/admin/audit/export").status_code, 401)

            user = _register(client, f"auditee-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            client.post(
                f"/api/admin/users/{user['id']}/note", json={"note": "重点观察", "tags": "vip"}
            )
            filtered = client.get("/api/admin/audit?action=user.set_note")
            self.assertEqual(filtered.status_code, 200, filtered.text)
            self.assertTrue(filtered.json()["items"])
            self.assertTrue(all("user.set_note" in item["action"] for item in filtered.json()["items"]))

    def test_dashboard_refresh_bypasses_cache(self):
        with TestClient(app) as client:
            store.invalidate_dashboard()
            self._admin_client(client)
            first = client.get("/api/admin/dashboard?days=7")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertFalse(first.json()["cached"])
            second = client.get("/api/admin/dashboard?days=7")
            self.assertTrue(second.json()["cached"])
            forced = client.get("/api/admin/dashboard?days=7&refresh=1")
            self.assertEqual(forced.status_code, 200, forced.text)
            self.assertFalse(forced.json()["cached"])


if __name__ == "__main__":
    unittest.main()
