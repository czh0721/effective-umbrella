import os
import tempfile
import unittest
from datetime import datetime, timedelta

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from ex_persona import credits, store  # noqa: E402


class CreditBatchTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username):
        return store.create_user(username, "h", "s")

    def test_grant_creates_batch_with_expiry(self):
        user = self._user("batch-grant")
        store.grant_credits(user["id"], 100, expires_days=30, source="admin")
        batches = store.list_credit_batches(user["id"])
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["remaining"], 100)
        self.assertEqual(batches[0]["source"], "admin")
        self.assertTrue(batches[0]["expires_at"])
        self.assertEqual(store.credits_summary(user["id"])["balance"], 100)

    def test_default_validity_days(self):
        user = self._user("batch-default")
        store.grant_credits(user["id"], 50)
        batch = store.list_credit_batches(user["id"])[0]
        created = datetime.fromisoformat(batch["created_at"])
        expires = datetime.fromisoformat(batch["expires_at"])
        self.assertEqual((expires - created).days, 30)

    def test_invalid_validity_raises(self):
        user = self._user("batch-bad-days")
        with self.assertRaises(store.InvalidValidityDays):
            store.grant_credits(user["id"], 10, expires_days=45)

    def test_deduct_consumes_earliest_expiry_first(self):
        user = self._user("batch-order")
        store.grant_credits(user["id"], 30, expires_days=30, reason="short")
        store.grant_credits(user["id"], 100, expires_days=365, reason="long")
        store.deduct_credits(user["id"], 50, reason="reply")
        batches = store.list_credit_batches(user["id"])
        by_reason = {item["reason"]: item for item in batches}
        self.assertEqual(by_reason["short"]["remaining"], 0)
        self.assertEqual(by_reason["long"]["remaining"], 80)
        self.assertEqual(store.get_credits(user["id"]), 80)

    def test_deduct_insufficient_across_batches(self):
        user = self._user("batch-insufficient")
        store.grant_credits(user["id"], 20, expires_days=30)
        store.grant_credits(user["id"], 20, expires_days=90)
        with self.assertRaises(store.InsufficientCredits):
            store.deduct_credits(user["id"], 50)
        self.assertEqual(store.get_credits(user["id"]), 40)

    def test_expire_zeroes_batches_and_writes_ledger(self):
        user = self._user("batch-expire")
        store.grant_credits(user["id"], 100, expires_days=30, reason="gift")
        future = (datetime.now().astimezone() + timedelta(days=31)).isoformat()
        result = store.expire_credit_batches(future)
        self.assertGreaterEqual(result["expired_batches"], 1)
        self.assertEqual(store.get_credits(user["id"]), 0)
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["balance"], 0)
        self.assertEqual(summary["expired"], 100)
        self.assertEqual(summary["used"], 0)
        ledger = store.list_credit_ledger(user["id"], limit=5)
        self.assertEqual(ledger[0]["reason"], store.EXPIRE_REASON)

    def test_expire_is_idempotent(self):
        user = self._user("batch-expire-twice")
        store.grant_credits(user["id"], 60, expires_days=30)
        future = (datetime.now().astimezone() + timedelta(days=40)).isoformat()
        first = store.expire_credit_batches(future)
        second = store.expire_credit_batches(future)
        self.assertGreaterEqual(first["expired_batches"], 1)
        self.assertEqual(second["expired_batches"], 0)
        self.assertEqual(store.get_credits(user["id"]), 0)
        expiries = [item for item in store.list_credit_ledger(user["id"], limit=20)
                    if item["reason"] == store.EXPIRE_REASON]
        self.assertEqual(len(expiries), 1)

    def test_expire_keeps_unexpired_batches(self):
        user = self._user("batch-expire-partial")
        store.grant_credits(user["id"], 50, expires_days=30)
        store.grant_credits(user["id"], 70, expires_days=365)
        future = (datetime.now().astimezone() + timedelta(days=31)).isoformat()
        store.expire_credit_batches(future)
        self.assertEqual(store.get_credits(user["id"]), 70)

    def test_summary_reports_expiring_soon(self):
        user = self._user("batch-soon")
        store.grant_credits(user["id"], 40, expires_days=30)
        soon = datetime.now().astimezone() + timedelta(hours=1)
        soon_iso = soon.isoformat()
        with store.connect() as conn:
            conn.execute(
                "UPDATE credit_batches SET expires_at = ? WHERE user_id = ?",
                (soon_iso, user["id"]),
            )
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["expiring_soon"], 40)
        self.assertEqual(summary["next_expiry"], soon_iso)

    def test_summary_reports_next_expiry_beyond_window(self):
        user = self._user("batch-next-far")
        store.grant_credits(user["id"], 80, expires_days=90)
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["expiring_soon"], 0)
        self.assertTrue(summary["next_expiry"])

    def test_ratio_ignores_expired_and_historical_grants(self):
        user = self._user("batch-ratio-live")
        store.grant_credits(user["id"], 100, expires_days=30)
        store.expire_credit_batches((datetime.now().astimezone() + timedelta(days=31)).isoformat())
        store.grant_credits(user["id"], 40, expires_days=90)
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["balance"], 40)
        self.assertEqual(summary["cycle_granted"], 40)
        self.assertEqual(summary["expired"], 100)
        self.assertAlmostEqual(summary["remaining_ratio"], 1.0, places=3)

    def test_migration_clear_counts_as_expired_not_used(self):
        user = self._user("batch-migration-usage")
        store.grant_credits(user["id"], 50, reason="gift")
        store.grant_credits(user["id"], -50, reason=store.MIGRATION_CLEAR_REASON)
        summary = store.credits_summary(user["id"])
        self.assertEqual(summary["balance"], 0)
        self.assertEqual(summary["used"], 0)
        self.assertEqual(summary["expired"], 50)

    def test_remind_once_within_window(self):
        user = self._user("batch-remind")
        store.grant_credits(user["id"], 30, expires_days=30)
        soon_iso = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
        with store.connect() as conn:
            conn.execute(
                "UPDATE credit_batches SET expires_at = ? WHERE user_id = ?",
                (soon_iso, user["id"]),
            )
        now = datetime.now().astimezone().isoformat()
        sent: list = []
        notify = lambda uid, kind, msg: sent.append((uid, kind, msg))  # noqa: E731
        first = store.remind_expiring_batches(now, notify=notify)
        mine = [item for item in sent if item[0] == user["id"]]
        second = store.remind_expiring_batches(now, notify=notify)
        mine_after = [item for item in sent if item[0] == user["id"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(mine_after), 1)
        self.assertIn("30", mine[0][2])
        self.assertGreaterEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)

    def test_remind_skips_outside_window(self):
        user = self._user("batch-remind-far")
        store.grant_credits(user["id"], 30, expires_days=365)
        result = store.remind_expiring_batches(
            notify=lambda *_: None
        )
        self.assertEqual(result["sent"], 0)

    def test_migration_clears_legacy_balance_once(self):
        user = self._user("batch-migration")
        with store.connect() as conn:
            conn.execute("UPDATE users SET credits = 500 WHERE id = ?", (user["id"],))
            conn.execute("DELETE FROM schema_meta WHERE key = 'credit_expiry_migrated'")
            store._migrate_credit_expiry(conn)
        self.assertEqual(store.get_credits(user["id"]), 0)
        ledger = store.list_credit_ledger(user["id"], limit=5)
        self.assertEqual(ledger[0]["delta"], -500)
        # 重复执行不产生第二次清零。
        with store.connect() as conn:
            store._migrate_credit_expiry(conn)
        self.assertEqual(len(store.list_credit_ledger(user["id"], limit=10)), 1)

    def test_worker_tick_expires(self):
        user = self._user("batch-worker")
        store.grant_credits(user["id"], 25, expires_days=30)
        future = (datetime.now().astimezone() + timedelta(days=31)).isoformat()
        worker = credits.CreditExpiryWorker(now_func=lambda: future)
        result = worker.tick()
        self.assertGreaterEqual(result["expired_batches"], 1)
        self.assertEqual(store.get_credits(user["id"]), 0)


if __name__ == "__main__":
    unittest.main()
