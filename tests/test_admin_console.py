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


if __name__ == "__main__":
    unittest.main()
