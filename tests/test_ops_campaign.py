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

from ex_persona import accounts, store  # noqa: E402
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


class OpsCampaignTestCase(unittest.TestCase):
    def _admin_client(self, client):
        username = f"admin-{uuid.uuid4().hex[:8]}"
        _make_admin(username)
        return _admin_login(client, username)["admin"]["id"]


class SegmentTest(OpsCampaignTestCase):
    def test_segments_resolve_expected_users(self):
        with TestClient(app) as client:
            tagged = _register(client, f"seg-tag-{uuid.uuid4().hex[:8]}")
            other = _register(client, f"seg-other-{uuid.uuid4().hex[:8]}")
            paid = _register(client, f"seg-paid-{uuid.uuid4().hex[:8]}")
            store.set_user_note(tagged["id"], "重点客户", "vip,cohort-a")
            store.record_order(paid["id"], 1, "体验包", 100, coins=0, idem=f"seg-{uuid.uuid4().hex}")
            self._admin_client(client)

            all_ids = set(store.segment_user_ids("all"))
            self.assertIn(tagged["id"], all_ids)
            self.assertIn(other["id"], all_ids)

            active_ids = set(store.segment_user_ids("active"))
            self.assertIn(tagged["id"], active_ids)

            new_ids = set(store.segment_user_ids("new"))
            self.assertIn(tagged["id"], new_ids)

            paid_ids = store.segment_user_ids("paid")
            self.assertIn(paid["id"], paid_ids)
            self.assertNotIn(other["id"], paid_ids)

            tag_ids = store.segment_user_ids("tag", "vip")
            self.assertIn(tagged["id"], tag_ids)
            self.assertNotIn(other["id"], tag_ids)

            manual_ids = store.segment_user_ids("manual", f"{tagged['id']},{other['id']}")
            self.assertEqual(sorted(manual_ids), sorted([tagged["id"], other["id"]]))

            self.assertEqual(store.segment_user_ids("manual", ""), [])
            self.assertGreaterEqual(store.count_segment("all"), 3)

    def test_disabled_user_excluded(self):
        with TestClient(app) as client:
            user = _register(client, f"seg-off-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            store.set_user_status(user["id"], "disabled", "测试")
            self.assertNotIn(user["id"], store.segment_user_ids("all"))

    def test_audience_count_endpoint(self):
        with TestClient(app) as client:
            user = _register(client, f"cnt-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            response = client.post(
                "/api/admin/audience/count",
                json={"audience": "tag", "audience_value": f"cnt-{user['id']}"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["count"], 0)

            response = client.post(
                "/api/admin/audience/count", json={"audience": "manual", "audience_value": str(user["id"])}
            )
            self.assertEqual(response.json()["count"], 1)


class AnnouncementCampaignTest(OpsCampaignTestCase):
    def test_create_patch_draft_and_state(self):
        with TestClient(app) as client:
            self._admin_client(client)
            created = client.post(
                "/api/admin/announcements",
                json={"title": "草稿公告", "body": "正文", "status": "draft", "audience": "all"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            ann = created.json()["announcement"]
            self.assertEqual(ann["status"], "draft")

            listing = client.get("/api/admin/announcements").json()["items"]
            row = next(item for item in listing if item["id"] == ann["id"])
            self.assertEqual(row["state"], "draft")
            self.assertEqual(row["audience_count"], 0)

            user = _register(client, f"ann-view-{uuid.uuid4().hex[:8]}")
            self.assertNotIn(ann["id"], [item["id"] for item in client.get("/api/announcements").json()["items"]])

            patched = client.patch(
                f"/api/admin/announcements/{ann['id']}",
                json={"status": "published", "title": "正式公告", "pinned": True},
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertEqual(patched.json()["announcement"]["title"], "正式公告")
            self.assertIn(
                ann["id"],
                [item["id"] for item in client.get("/api/announcements").json()["items"]],
            )
            self.assertGreaterEqual(patched.json()["announcement"]["audience_count"], 1)
            self.assertTrue(user["id"])

    def test_time_window_and_validation(self):
        with TestClient(app) as client:
            self._admin_client(client)
            future = client.post(
                "/api/admin/announcements",
                json={"title": "未来公告", "starts_at": "2999-01-01T00:00", "audience": "all"},
            ).json()["announcement"]
            user = _register(client, f"ann-time-{uuid.uuid4().hex[:8]}")
            self.assertNotIn(future["id"], [i["id"] for i in client.get("/api/announcements").json()["items"]])
            rows = client.get("/api/admin/announcements").json()["items"]
            self.assertEqual(next(i for i in rows if i["id"] == future["id"])["state"], "scheduled")
            self.assertTrue(user)

            bad = client.post(
                "/api/admin/announcements",
                json={"title": "时间错", "starts_at": "2030-01-02T00:00", "ends_at": "2030-01-01T00:00"},
            )
            self.assertEqual(bad.status_code, 400)

    def test_audience_targeted_visibility_and_reach(self):
        with TestClient(app) as client:
            member = _register(client, f"ann-member-{uuid.uuid4().hex[:8]}")
            store.set_user_note(member["id"], "", "ann-segment")
            self._admin_client(client)
            created = client.post(
                "/api/admin/announcements",
                json={
                    "title": "定向公告",
                    "body": "仅标签可见",
                    "audience": "tag",
                    "audience_value": "ann-segment",
                },
            ).json()["announcement"]

            member_view = client.get("/api/announcements").json()["items"]
            self.assertIn(created["id"], [i["id"] for i in member_view])

            reach = client.get(f"/api/admin/announcements/{created['id']}/reach").json()
            self.assertEqual(reach["audience_count"], 1)
            self.assertEqual(reach["read"], 1)
            first_read = client.get(
                f"/api/admin/announcements/{created['id']}/reach/users?state=read"
            ).json()
            self.assertEqual(first_read["total"], 1)
            self.assertEqual(first_read["items"][0]["id"], member["id"])

            _register(client, f"ann-out-{uuid.uuid4().hex[:8]}")
            outsider_view = client.get("/api/announcements").json()["items"]
            self.assertNotIn(created["id"], [i["id"] for i in outsider_view])

    def test_read_receipt_is_idempotent(self):
        with TestClient(app) as client:
            user = _register(client, f"ann-read-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            ann = client.post(
                "/api/admin/announcements", json={"title": "回执", "audience": "all"}
            ).json()["announcement"]
            client.get("/api/announcements")
            first = store.reach_stats(ann["id"])
            client.get("/api/announcements")
            second = store.reach_stats(ann["id"])
            self.assertEqual(first["read"], 1)
            self.assertEqual(second["read"], 1)
            self.assertTrue(user)


class NoticeTemplateTest(OpsCampaignTestCase):
    def test_template_crud_and_rendering(self):
        with TestClient(app) as client:
            user = _register(client, f"tpl-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            templates = client.get("/api/admin/notice-templates")
            self.assertEqual(templates.status_code, 200)
            self.assertIn("{username}", templates.json()["variables"])

            created = client.post(
                "/api/admin/notice-templates",
                json={"name": "积分提醒", "body": "你好 {nickname}，当前积分 {credits}"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            tid = created.json()["template"]["id"]

            patched = client.patch(
                f"/api/admin/notice-templates/{tid}", json={"body": "{username} 你有 {coins} 念念币"}
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertIn("{coins}", patched.json()["template"]["body"])

            listed = client.get("/api/admin/notice-templates").json()["items"]
            self.assertIn(tid, [t["id"] for t in listed])

            sent = client.post(
                "/api/admin/notices",
                json={
                    "message": "你好 {nickname}，当前积分 {credits}",
                    "audience": "manual",
                    "audience_value": str(user["id"]),
                    "render_variables": True,
                },
            )
            self.assertEqual(sent.status_code, 200, sent.text)
            alerts = store.list_alerts(user["id"], limit=5)
            rendered = next(a for a in alerts if "当前积分" in a["message"])
            self.assertNotIn("{nickname}", rendered["message"])
            self.assertNotIn("{credits}", rendered["message"])

            deleted = client.delete(f"/api/admin/notice-templates/{tid}")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(client.delete(f"/api/admin/notice-templates/{tid}").status_code, 404)

    def test_notice_history_and_stats(self):
        with TestClient(app) as client:
            user = _register(client, f"hist-{uuid.uuid4().hex[:8]}")
            self._admin_client(client)
            sent = client.post(
                "/api/admin/notices",
                json={"message": "状态通知", "audience": "manual", "audience_value": str(user["id"])},
            )
            self.assertEqual(sent.status_code, 200, sent.text)
            notice_id = sent.json()["notice_id"]

            history = client.get("/api/admin/notices").json()["items"]
            row = next(item for item in history if item["id"] == notice_id)
            self.assertEqual(row["delivered"], 1)
            self.assertEqual(row["read"], 0)

            stats = client.get(f"/api/admin/notices/{notice_id}").json()
            self.assertEqual(stats["delivered"], 1)
            self.assertEqual(stats["unread"], 1)

            client.post("/api/notifications/read")
            stats2 = client.get(f"/api/admin/notices/{notice_id}").json()
            self.assertEqual(stats2["read"], 1)
            self.assertEqual(stats2["unread"], 0)

    def test_empty_audience_rejected(self):
        with TestClient(app) as client:
            self._admin_client(client)
            response = client.post(
                "/api/admin/notices",
                json={"message": "空受众", "audience": "tag", "audience_value": "no-such-tag-xyz"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("目标受众为空", response.json()["detail"])


class OpsCampaignGuardTest(OpsCampaignTestCase):
    def test_admin_endpoints_require_admin(self):
        with TestClient(app) as client:
            _register(client, f"guard-{uuid.uuid4().hex[:8]}")
            self.assertEqual(client.post("/api/admin/audience/count", json={"audience": "all"}).status_code, 401)
            self.assertEqual(client.get("/api/admin/announcements").status_code, 401)
            self.assertEqual(client.get("/api/admin/notice-templates").status_code, 401)
            self.assertEqual(client.get("/api/admin/notices").status_code, 401)

    def test_audit_records_written(self):
        with TestClient(app) as client:
            self._admin_client(client)
            client.post("/api/admin/announcements", json={"title": "审计公告"})
            client.post("/api/admin/notice-templates", json={"name": "审计模板", "body": "x"})
            has_create = store.list_audit(limit=50, action="announcement.create")
            self.assertTrue(any(row["detail"].startswith("title=审计公告") for row in has_create))
            has_template = store.list_audit(limit=50, action="notice_template.create")
            self.assertTrue(has_template)


if __name__ == "__main__":
    unittest.main()
