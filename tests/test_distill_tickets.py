import contextlib
import os
import tempfile
import unittest


@contextlib.contextmanager
def _isolated_db_path():
    old = os.environ.get("PERSONA_DB_PATH")
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    os.environ["PERSONA_DB_PATH"] = handle.name
    store._initialized = False
    try:
        store.init_db()
        yield
    finally:
        if old is None:
            os.environ.pop("PERSONA_DB_PATH", None)
        else:
            os.environ["PERSONA_DB_PATH"] = old
        store._initialized = False
        store.init_db()


_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from ex_persona import store  # noqa: E402


class DistillCreditTest(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _user(self, username, credits=0):
        user = store.create_user(username, "h", "s")
        if credits:
            store.grant_credits(user["id"], credits, reason="测试发放", source="gift")
        return user

    def test_default_cost_is_100(self):
        self.assertEqual(store.distill_credit_cost(), 100)

    def test_new_user_has_no_tickets_by_default(self):
        user = self._user("ticket-default")
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)

    def test_reserve_requires_credits(self):
        user = self._user("credit-reserve-none")
        cost = store.distill_credit_cost()
        with self.assertRaises(store.InsufficientCredits):
            store.reserve_distill_credits(user["id"], "task-x")
        self.assertEqual(store.get_credits(user["id"]), 0)
        self.assertGreater(cost, 0)

    def test_reserve_then_refund(self):
        user = self._user("credit-refund", credits=500)
        cost = store.distill_credit_cost()
        store.reserve_distill_credits(user["id"], "task-1")
        self.assertEqual(store.get_credits(user["id"]), 500 - cost)
        store.refund_distill_credits(user["id"], "task-1")
        self.assertEqual(store.get_credits(user["id"]), 500)

    def test_refund_is_idempotent(self):
        user = self._user("credit-refund-idem", credits=500)
        cost = store.distill_credit_cost()
        store.reserve_distill_credits(user["id"], "task-2")
        first = store.refund_distill_credits(user["id"], "task-2")
        second = store.refund_distill_credits(user["id"], "task-2")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(store.get_credits(user["id"]), 500)
        self.assertEqual(first["amount"], cost)

    def test_reconcile_refunds_interrupted_task(self):
        user = self._user("credit-reconcile", credits=500)
        cost = store.distill_credit_cost()
        store.create_task("task-stale", user["id"], None, "distill")
        store.reserve_distill_credits(user["id"], "task-stale")
        self.assertEqual(store.get_credits(user["id"]), 500 - cost)
        result = store.reconcile_distill_credits()
        self.assertEqual(result["refunded"], 1)
        self.assertEqual(store.get_credits(user["id"]), 500)
        task = store.get_task("task-stale")
        self.assertEqual(task["status"], "error")
        self.assertEqual(task["error_kind"], "interrupted")

    def _reset_migration(self):
        with store.connect() as conn:
            conn.execute("DELETE FROM schema_meta WHERE key = 'distill_credits_v1'")
        store._initialized = False
        store.init_db()

    def test_migration_converts_ticket_balance_to_credits(self):
        user = self._user("migrate-ticket")
        store.grant_distill_tickets(user["id"], 3, reason="历史发放", actor="test")
        with store.connect() as conn:
            conn.execute("DELETE FROM credit_ledger WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM credit_batches WHERE user_id = ?", (user["id"],))
        self._reset_migration()
        cost = store.distill_credit_cost()
        self.assertEqual(store.get_credits(user["id"]), 3 * cost)
        self.assertEqual(store.get_distill_tickets(user["id"]), 0)
        ledger = store.list_credit_ledger(user["id"], limit=5)
        self.assertTrue(any(store.DISTILL_CONVERT_REASON in item["reason"] for item in ledger))

    def test_migration_is_idempotent(self):
        user = self._user("migrate-idem")
        store.grant_distill_tickets(user["id"], 2, reason="历史发放", actor="test")
        with store.connect() as conn:
            conn.execute("DELETE FROM credit_ledger WHERE user_id = ?", (user["id"],))
            conn.execute("DELETE FROM credit_batches WHERE user_id = ?", (user["id"],))
        self._reset_migration()
        cost = store.distill_credit_cost()
        first = store.get_credits(user["id"])
        self.assertEqual(first, 2 * cost)
        store._initialized = False
        store.init_db()
        self.assertEqual(store.get_credits(user["id"]), first)

    def test_migration_converts_package_bonus_tickets(self):
        package = store.upsert_credit_package(
            None, "迁移券包", 100, 0, "", 1, True, coins=10, validity_days=30,
            bonus_credits=0, bonus_tickets=2,
        )
        self._reset_migration()
        cost = store.distill_credit_cost()
        updated = next(
            p for p in store.list_credit_packages() if p["id"] == package["id"]
        )
        self.assertEqual(updated["bonus_tickets"], 0)
        self.assertEqual(updated["bonus_credits"], 2 * cost)

    def _seed_gift_then_migrate(self, new_user_gift, gift, cost):
        store.set_platform_config(
            "", "", "deepseek-chat", True, 20, new_user_gift,
            default_credit_days=30, distill_credit_cost=cost,
            distill_ticket_price=60, distill_ticket_gift=gift,
        )
        with store.connect() as conn:
            conn.execute("DELETE FROM schema_meta WHERE key = 'distill_gift_merged_v1'")
        store._initialized = False
        store.init_db()
        return store.get_platform_config_row()

    def test_migration_merges_registration_distill_gift(self):
        with _isolated_db_path():
            row = self._seed_gift_then_migrate(100, 1, 100)
            self.assertEqual(int(row["new_user_gift"]), 200)
            self.assertEqual(int(row["distill_ticket_gift"]), 0)

    def test_merge_gift_migration_is_idempotent(self):
        with _isolated_db_path():
            row = self._seed_gift_then_migrate(100, 2, 100)
            self.assertEqual(int(row["new_user_gift"]), 300)
            store._initialized = False
            store.init_db()
            self.assertEqual(int(store.get_platform_config_row()["new_user_gift"]), 300)


if __name__ == "__main__":
    unittest.main()
