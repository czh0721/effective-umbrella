import json
import os
import tempfile
import unittest
import uuid

if "PERSONA_DATA_DIR" not in os.environ:
    _TMP = tempfile.TemporaryDirectory()
    os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ.setdefault("PERSONA_SECRET_KEY", "unit-test-secret")
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, observability, store  # noqa: E402
from ex_persona.webapp import app  # noqa: E402


def _make_admin(username, password="Password123!"):
    return accounts.create_admin(username, password)


def _admin_login(client, username, password="Password123!"):
    response = client.post(
        "/api/admin/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _register(client, username):
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "Password123!"}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


class AdminOpsTestCase(unittest.TestCase):
    def _admin_client(self, client):
        username = f"admin-{uuid.uuid4().hex[:8]}"
        _make_admin(username)
        data = _admin_login(client, username)
        return data["admin"]["id"]


class SessionManagementTest(AdminOpsTestCase):
    def test_list_revoke_and_revoke_all_sessions(self):
        with TestClient(app) as client:
            user = _register(client, f"sess-{uuid.uuid4().hex[:8]}")
            client.post("/api/auth/login", json={"username": user["username"], "password": "Password123!"})
            self._admin_client(client)

            listed = client.get(f"/api/admin/users/{user['id']}/sessions")
            self.assertEqual(listed.status_code, 200, listed.text)
            items = listed.json()["items"]
            self.assertTrue(items)
            first = items[0]
            self.assertTrue(first["token_prefix"])
            self.assertNotIn("token", first)

            revoked = client.post(
                f"/api/admin/users/{user['id']}/sessions/{first['token_prefix']}/revoke"
            )
            self.assertEqual(revoked.status_code, 200, revoked.text)
            remaining = client.get(f"/api/admin/users/{user['id']}/sessions").json()["items"]
            self.assertNotIn(first["token_prefix"], [row["token_prefix"] for row in remaining])

            all_revoked = client.post(f"/api/admin/users/{user['id']}/sessions/revoke-all")
            self.assertEqual(all_revoked.status_code, 200, all_revoked.text)
            self.assertEqual(client.get(f"/api/admin/users/{user['id']}/sessions").json()["items"], [])

            audits = store.list_audit(limit=50, action="security.session_revoke")
            self.assertTrue(audits)
            self.assertTrue(any(row["target"] == str(user["id"]) for row in audits))

    def test_missing_session_returns_404(self):
        with TestClient(app) as client:
            user = _register(client, f"sess404-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            missing = client.post(f"/api/admin/users/{user['id']}/sessions/deadbeef/revoke")
            self.assertEqual(missing.status_code, 404)

    def test_reset_user_totp(self):
        with TestClient(app) as client:
            user = _register(client, f"totp-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            store.set_user_totp(user["id"], "SECRETSECRET", True)
            self.assertTrue(store.get_user(user["id"])["totp_enabled"])

            reset = client.post(f"/api/admin/users/{user['id']}/totp/reset")
            self.assertEqual(reset.status_code, 200, reset.text)
            refreshed = store.get_user(user["id"])
            self.assertFalse(refreshed["totp_enabled"])
            self.assertEqual(refreshed["totp_secret"], "")
            audits = store.list_audit(limit=50, action="security.reset_totp")
            self.assertTrue(any(row["target"] == str(user["id"]) for row in audits))

    def test_reset_totp_without_binding_returns_400(self):
        with TestClient(app) as client:
            user = _register(client, f"nototp-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            response = client.post(f"/api/admin/users/{user['id']}/totp/reset")
            self.assertEqual(response.status_code, 400)


class AlertCenterTest(AdminOpsTestCase):
    def test_filter_and_single_read(self):
        with TestClient(app) as client:
            admin_id = self._admin_client(client)
            store.add_admin_alert(admin_id, "crisis", "用户触发兜底", target="user:42")
            store.add_admin_alert(admin_id, "platform", "平台模型未启用", target="")

            unread = client.get("/api/admin/alerts?status=unread")
            self.assertEqual(unread.status_code, 200, unread.text)
            self.assertTrue(unread.json()["items"])

            kinds = client.get("/api/admin/alerts?kind=crisis")
            items = kinds.json()["items"]
            self.assertTrue(items)
            self.assertTrue(all(item["kind"] == "crisis" for item in items))
            target_item = next(item for item in items if item["target"] == "user:42")

            read = client.post(f"/api/admin/alerts/{target_item['id']}/read")
            self.assertEqual(read.status_code, 200, read.text)
            again = client.post(f"/api/admin/alerts/{target_item['id']}/read")
            self.assertEqual(again.status_code, 404)

            read_rows = client.get("/api/admin/alerts?status=read").json()["items"]
            self.assertIn(target_item["id"], [row["id"] for row in read_rows])


class ApplicationLogTest(AdminOpsTestCase):
    def _write_log(self, payload: dict) -> None:
        observability.log_dir().mkdir(parents=True, exist_ok=True)
        with observability.log_file_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def test_log_tail_filters(self):
        with TestClient(app) as client:
            self._admin_client(client)
            self._write_log({"ts": "2026-09-18T10:00:00", "level": "INFO", "logger": "t", "msg": "hello-alpha"})
            self._write_log({"ts": "2026-09-18T10:00:01", "level": "ERROR", "logger": "t", "msg": "boom-beta"})

            all_rows = client.get("/api/admin/system/logs")
            self.assertEqual(all_rows.status_code, 200, all_rows.text)
            self.assertTrue(all_rows.json()["items"])

            errors = client.get("/api/admin/system/logs?level=ERROR")
            messages = [row["msg"] for row in errors.json()["items"]]
            self.assertIn("boom-beta", messages)
            self.assertNotIn("hello-alpha", messages)

            keyword = client.get("/api/admin/system/logs?keyword=alpha")
            self.assertTrue(all("alpha" in row["msg"] for row in keyword.json()["items"]))

    def test_read_log_tail_missing_file(self):
        path = observability.log_file_path()
        original = path.read_bytes() if path.exists() else None
        if path.exists():
            path.write_bytes(b"")
        try:
            self.assertEqual(observability.read_log_tail(), [])
        finally:
            if original is not None:
                path.write_bytes(original)


class RedemptionBatchTest(AdminOpsTestCase):
    def test_export_and_batch_void(self):
        with TestClient(app) as client:
            self._admin_client(client)
            created = store.create_redemption_codes(3, 100, batch="ops", actor="test")
            unused_ids = [row["id"] for row in created]
            used = created[0]
            user = _register(client, f"redeem-{uuid.uuid4().hex[:8]}")
            store.redeem_code(user["id"], used["code"])

            export = client.get("/api/admin/redemption-codes/export")
            self.assertEqual(export.status_code, 200, export.text)
            self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("兑换码", export.content.decode("utf-8"))

            result = client.post(
                "/api/admin/redemption-codes/batch-void",
                json={"ids": unused_ids},
            )
            self.assertEqual(result.status_code, 200, result.text)
            body = result.json()
            self.assertEqual(body["voided"], 2)
            self.assertEqual(body["skipped"], 1)

            rows = {row["id"]: row for row in store.export_redemption_codes()}
            self.assertEqual(rows[unused_ids[1]]["status"], "void")
            self.assertEqual(rows[used["id"]]["status"], "used")

    def test_batch_void_all_matching_and_empty(self):
        with TestClient(app) as client:
            self._admin_client(client)
            store.create_redemption_codes(2, 50, batch="all", actor="test")
            empty = client.post("/api/admin/redemption-codes/batch-void", json={})
            self.assertEqual(empty.status_code, 400)

            result = client.post(
                "/api/admin/redemption-codes/batch-void",
                json={"all_matching": True, "status": "unused"},
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertGreaterEqual(result.json()["voided"], 2)

    def test_audit_written_for_batch_void(self):
        with TestClient(app) as client:
            self._admin_client(client)
            created = store.create_redemption_codes(1, 10, actor="test")
            client.post(
                "/api/admin/redemption-codes/batch-void",
                json={"ids": [created[0]["id"]]},
            )
            audits = store.list_audit(limit=50, action="redemption.batch_void")
            self.assertTrue(audits)


class AdminOpsGuardTest(unittest.TestCase):
    def test_endpoints_require_admin(self):
        with TestClient(app) as client:
            user = _register(client, f"guard-{uuid.uuid4().hex[:8]}")
            self.assertEqual(client.get(f"/api/admin/users/{user['id']}/sessions").status_code, 401)
            self.assertEqual(client.get("/api/admin/system/logs").status_code, 401)
            self.assertEqual(client.get("/api/admin/redemption-codes/export").status_code, 401)
            self.assertEqual(
                client.post("/api/admin/redemption-codes/batch-void", json={"ids": [1]}).status_code,
                401,
            )


if __name__ == "__main__":
    unittest.main()
