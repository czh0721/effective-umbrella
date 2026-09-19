import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import accounts, crypto, store, workspace  # noqa: E402
from ex_persona.webapp import app, build_config  # noqa: E402


class UnitTest(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        salt, digest = accounts.hash_password("hunter2hunter2")
        self.assertTrue(accounts.verify_password("hunter2hunter2", salt, digest))
        self.assertFalse(accounts.verify_password("wrong", salt, digest))

    def test_register_duplicate(self):
        store.init_db()
        accounts.register("unit-user", "Password123!")
        with self.assertRaises(accounts.AccountError):
            accounts.register("unit-user", "Password123!")

    def test_short_password_rejected(self):
        with self.assertRaises(accounts.AccountError):
            accounts.validate_credentials("someone", "short")

    def test_crypto_roundtrip_and_mask(self):
        token = crypto.encrypt("sk-super-secret-value")
        self.assertNotIn("super", token)
        self.assertEqual(crypto.decrypt(token), "sk-super-secret-value")
        self.assertEqual(crypto.decrypt("garbage"), "")
        masked = crypto.mask("sk-super-secret-value")
        self.assertNotIn("super", masked)
        self.assertTrue(masked.endswith("alue"))

    def test_clean_reply_drops_leaked_prompt(self):
        from ex_persona.agent import clean_reply

        leaked = (
            "# SKILL: 以「林晚」的方式聊天\n\n"
            "## 角色\n你现在就是 林晚。用 ta 的语气、用词和情绪跟对方发微信。\n\n"
            "## 回复规则\n1. 像真实微信聊天一样短。"
        )
        self.assertEqual(clean_reply(leaked), "")
        self.assertEqual(clean_reply("【历史真实对话参考】\n对方：在吗"), "")
        self.assertEqual(clean_reply("晚安，早点休息"), "晚安，早点休息")
        self.assertEqual(clean_reply("[[SILENCE]]"), "[[SILENCE]]")

    def test_history_for_llm_drops_fallback_turns(self):
        from ex_persona.webapp import history_for_llm

        settings = {"model": {"fallback_reply": "哈哈，刚在忙，晚点回你。"}}
        stored = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "哈哈，刚在忙，晚点回你。"},
            {"role": "user", "content": "在吗"},
            {"role": "assistant", "content": "在呢"},
            {"role": "user", "content": "睡了没"},
        ]
        # 兜底回复连同它那条用户消息一起丢弃，避免留下「没有回应的用户消息」；
        # 末尾还没被回复的用户消息保留。
        self.assertEqual(
            history_for_llm(stored, settings),
            [
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "在呢"},
                {"role": "user", "content": "睡了没"},
            ],
        )
        # 连续多条没有回应的用户消息，只保留最后一条。
        orphans = [
            {"role": "user", "content": "66"},
            {"role": "user", "content": "6"},
            {"role": "user", "content": "人呢"},
        ]
        self.assertEqual(
            history_for_llm(orphans, settings), [{"role": "user", "content": "人呢"}]
        )

    def test_looks_garbled_detects_degenerate_output(self):
        from ex_persona.agent import looks_garbled

        gibberish = (
            "噗……是真的好看得很🌾👀你还搁这叫呢*́◇4p͏ྱ7ོอาMETA\n"
            "😙づ你也真有时间好玩𤐁ཇづシ＇给🌵ོy̥我⚡̈退a皮蛋*̈*۶೭㋅ာษฐ๓ு⁹是🍨🎍۶"
            "不是你咋在这✔丫﷼ఠﮛ˶SmartลातཀድᎶ๔ژ۹۷ツై६🙄✫৭ل︒¼"
        )
        self.assertTrue(looks_garbled(gibberish))
        for good in (
            "在在在！我跟你说我刚想找你！！",
            "哈哈，刚在忙，晚点回你。",
            "在在在！！怎么啦怎么啦(๑•̀ㅂ•́)و✧",
            "林晚。你这话问得，像我们刚认识一样。",
            "还行吧 就那样\n\n就……早上赖了会儿床，磨蹭到快中午才起来",
            "嗯嗯，晚点聊~ 🥺 抱抱你🤗",
        ):
            self.assertFalse(looks_garbled(good), good)

    def test_reply_retries_and_drops_garbled_output(self):
        from ex_persona import agent as agent_module
        from ex_persona.agent import PersonaAgent
        from ex_persona.config import LLMConfig

        class _Agent(PersonaAgent):
            def __init__(self, outputs):
                self.config = LLMConfig(
                    api_key="k", base_url="https://x/v1", model="m", fallback_reply="兜底"
                )
                self._outputs = list(outputs)
                self.last_error = None
                self.last_fallback = False

            def build_system(self, query, extra_context=""):
                return "system"

        calls = []

        def fake_chat(_config, _messages, temperature=None, max_tokens=None):
            calls.append(temperature)
            return outputs.pop(0)

        original = agent_module.chat
        agent_module.chat = fake_chat
        try:
            outputs = ["жかڇาᎶضጥ٩۷ツై६🙄✫৭ل︒¼Ⳇښڢښژ", "在呢，怎么啦"]
            self.assertEqual(_Agent(outputs).reply("在吗", temperature=1.3), "在呢，怎么啦")
            self.assertEqual(calls, [1.3, 0.6])

            outputs = ["Ꮆضጥ٩۷ツై६🙄✫৭ل︒¼ⳆښڢښژཀድᎶ๔ژ۹۷ツై", "ڇาᎶضጥ٩۷ツై६🙄✫৭ل︒¼Ⳇښڢښژཀ"]
            self.assertEqual(_Agent(outputs).reply("在吗", temperature=1.3), "")
        finally:
            agent_module.chat = original

    def test_bridge_home_is_absolute(self):
        from ex_persona.wechat import WeChatBridge

        bridge = WeChatBridge(1, "data/users/1/home", 8000)
        self.assertTrue(bridge.home.is_absolute())
        self.assertEqual(bridge.config_path.parent, bridge.home / ".weclaw")

    def test_bridge_autostart_when_bound(self):
        from ex_persona.wechat import WeChatBridge

        root = Path(_TMP.name) / "autostart"
        bridge = WeChatBridge(2, root, 8000)
        started: list[bool] = []
        bridge.is_running = lambda: False  # type: ignore[method-assign]
        bridge.start_bridge = lambda: started.append(True) or {"code": 0, "output": "ok"}  # type: ignore[method-assign]
        bridge._run = lambda *a, **k: (0, "stopped")  # type: ignore[method-assign]

        self.assertFalse(bridge.is_bound())
        bridge.maybe_autostart()
        self.assertEqual(started, [])

        accounts = root / ".weclaw" / "accounts"
        accounts.mkdir(parents=True)
        (accounts / "abc-im-bot.json").write_text("{}", encoding="utf-8")
        self.assertTrue(bridge.is_bound())
        bridge.maybe_autostart()
        self.assertEqual(started, [True])

        bridge.stop_bridge(explicit=True)
        self.assertFalse(bridge._autostart)

    def test_bridge_stop_only_affects_own_home(self):
        import subprocess
        import sys
        import time

        from ex_persona.wechat import WeChatBridge

        root = Path(_TMP.name) / "own-home"
        root.mkdir(parents=True, exist_ok=True)
        other_home = Path(_TMP.name) / "other-home"
        other_home.mkdir(parents=True, exist_ok=True)
        bridge = WeChatBridge(9, root, 8000)

        def spawn(home):
            env = dict(os.environ)
            env["HOME"] = str(home)
            return subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)", "weclaw", "start", "-f"],
                env=env,
            )

        own = spawn(bridge.home)
        other = spawn(other_home)
        try:
            for _ in range(50):
                if bridge._home_bridge_alive():
                    break
                time.sleep(0.1)
            self.assertTrue(bridge._home_bridge_alive(), "应识别到本用户 HOME 下的 weclaw 实例")
            self.assertTrue(bridge.is_running())
            bridge.stop_bridge(explicit=True)
            own.wait(timeout=5)
            self.assertFalse(bridge._home_bridge_alive())
            self.assertIsNone(other.poll(), "停止转发不应误杀其他用户的桥接进程")
        finally:
            for proc in (own, other):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_bridge_never_calls_global_weclaw_stop(self):
        from ex_persona import wechat as wechat_mod
        from ex_persona.wechat import WeChatBridge

        root = Path(_TMP.name) / "no-global-stop"
        root.mkdir(parents=True, exist_ok=True)
        bridge = WeChatBridge(10, root, 8000)
        calls: list[tuple] = []
        bridge._run = lambda *a, **k: calls.append(a) or (0, "should-not-run")  # type: ignore[method-assign]

        class DummyProc:
            returncode = None
            stdout: list = []

            def poll(self):
                return None

            def terminate(self):
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.returncode = -9

        original = wechat_mod.subprocess.Popen
        wechat_mod.subprocess.Popen = lambda *a, **k: DummyProc()  # type: ignore[assignment]
        try:
            self.assertEqual(bridge.start_bridge()["code"], 0)
            self.assertEqual(bridge.start_bridge()["output"], "已在运行")
        finally:
            wechat_mod.subprocess.Popen = original  # type: ignore[assignment]
        bridge.stop_bridge(explicit=True)

        self.assertEqual(calls, [], "不应调用会全局 pkill 的 weclaw stop")
        self.assertFalse(bridge._autostart)

    def test_bridge_login_restarts_forwarding(self):
        from ex_persona.wechat import WeChatBridge

        bridge = WeChatBridge(11, Path(_TMP.name) / "relogin", 8000)
        forced: list[bool] = []
        bridge.start_bridge = lambda force=False: forced.append(force) or {  # type: ignore[method-assign]
            "code": 0,
            "output": "ok",
        }

        class LoginProc:
            stdout = None

            def wait(self):
                return 0

        bridge._phase = "starting"
        bridge._pump_login(LoginProc())
        self.assertEqual(forced, [True], "重新登录成功后必须强制重启桥接以加载新凭据")

    def test_bridge_detects_expired_session(self):
        from ex_persona.wechat import WeChatBridge

        events: list[tuple[int, str]] = []
        bridge = WeChatBridge(
            12,
            Path(_TMP.name) / "expired",
            8000,
            on_event=lambda uid, event: events.append((uid, event)),
        )
        bridge._run = lambda *a, **k: (0, "weclaw is not running")  # type: ignore[method-assign]
        bridge._home_bridge_alive = lambda: True  # type: ignore[method-assign]
        bridge._terminate_home_processes = lambda: None  # type: ignore[method-assign]
        with bridge._lock:
            bridge._bridge_log = (
                "[monitor] WARNING: WeChat session expired and cannot be auto-recovered."
                " Run `weclaw login` to re-authenticate."
            )

        status = bridge.status(with_svg=False)
        self.assertFalse(status["running"], "会话过期后不应报告在线")
        self.assertEqual(status["phase"], "expired")
        self.assertTrue(status["expired"])
        self.assertIn((12, "expired"), events)
        self.assertFalse(bridge.is_running())

        # 失效状态下 watchdog 不应反复尝试拉起。
        started: list[bool] = []
        bridge.is_bound = lambda: True  # type: ignore[method-assign]
        bridge.start_bridge = lambda force=False: started.append(force) or {"code": 0}  # type: ignore[method-assign]
        bridge.maybe_autostart()
        self.assertEqual(started, [])

    def test_bridge_event_persists_phase_and_alerts(self):
        from ex_persona import webapp as webapp_module

        store.init_db()
        user_id = 900000001
        home = str(Path(_TMP.name) / "event-home")
        store.upsert_wechat_binding(
            user_id, bridge_token=f"tok-{user_id}", home_dir=home, phase="running"
        )
        webapp_module._on_bridge_event(user_id, "expired")
        self.assertEqual(store.get_wechat_binding(user_id)["phase"], "expired")

    def test_autostart_only_when_forwarding(self):
        from ex_persona import webapp as webapp_module

        store.init_db()
        user_id = 900000002
        home = str(Path(_TMP.name) / "phase-home")
        store.upsert_wechat_binding(
            user_id, bridge_token=f"tok-{user_id}", home_dir=home, phase="logged-in"
        )

        class StubBridge:
            def __init__(self) -> None:
                self.autostarted = 0

            def maybe_autostart(self) -> None:
                self.autostarted += 1

        created: dict[int, StubBridge] = {}

        def fake_get(uid, home_dir, token):
            stub = StubBridge()
            created[uid] = stub
            return stub

        original = webapp_module.manager.get
        webapp_module.manager.get = fake_get  # type: ignore[method-assign]
        try:
            webapp_module._autostart_bridges()
            self.assertNotIn(user_id, created, "用户已停止转发的渠道不应被自动打开")

            store.set_binding_phase(user_id, "running")
            webapp_module._autostart_bridges()
            self.assertIn(user_id, created)
            self.assertEqual(created[user_id].autostarted, 1)
        finally:
            webapp_module.manager.get = original  # type: ignore[method-assign]

    def test_manager_stop_all_does_not_persist_intent(self):
        from ex_persona.wechat import WeChatBridge, WeChatManager

        manager = WeChatManager(8000)
        events: list[str] = []
        bridge = WeChatBridge(
            13,
            Path(_TMP.name) / "stopall",
            8000,
            on_event=lambda uid, event: events.append(event),
        )
        bridge._terminate_home_processes = lambda: None  # type: ignore[method-assign]
        manager._bridges[13] = bridge

        manager.stop_all()
        self.assertNotIn("logged-in", events, "服务停机清理不应写成用户主动关闭")

    def test_wants_forwarding_gate(self):
        from ex_persona.webapp import _wants_forwarding

        self.assertTrue(_wants_forwarding({"phase": "running"}))
        self.assertTrue(_wants_forwarding({"phase": "failed"}))
        self.assertFalse(_wants_forwarding({"phase": "logged-in"}))
        self.assertFalse(_wants_forwarding({"phase": "expired"}))
        self.assertFalse(_wants_forwarding({}))

    def test_prune_stale_accounts_keeps_latest(self):
        from ex_persona.wechat import WeChatBridge

        root = Path(_TMP.name) / "prune"
        accounts = root / ".weclaw" / "accounts"
        accounts.mkdir(parents=True, exist_ok=True)
        old = accounts / "old-im-bot.json"
        old.write_text("{}", encoding="utf-8")
        (accounts / "old-im-bot.sync.json").write_text("{}", encoding="utf-8")
        os.utime(old, (1, 1))
        fresh = accounts / "new-im-bot.json"
        fresh.write_text("{}", encoding="utf-8")
        bridge = WeChatBridge(21, root, 8000)

        self.assertEqual(bridge.prune_stale_accounts(), 2)
        self.assertEqual([p.name for p in bridge.account_files()], ["new-im-bot.json"])
        self.assertTrue((accounts / "_stale" / "old-im-bot.json").exists())
        self.assertTrue(fresh.exists(), "最新账号必须保留")

    def test_status_with_multiple_accounts_recovers_instead_of_expiring(self):
        import time

        from ex_persona.wechat import WeChatBridge

        events: list[str] = []
        root = Path(_TMP.name) / "multi-account"
        accounts = root / ".weclaw" / "accounts"
        accounts.mkdir(parents=True, exist_ok=True)
        old = accounts / "a-im-bot.json"
        old.write_text("{}", encoding="utf-8")
        os.utime(old, (1, 1))
        (accounts / "b-im-bot.json").write_text("{}", encoding="utf-8")
        bridge = WeChatBridge(22, root, 8000, on_event=lambda uid, event: events.append(event))
        bridge._run = lambda *a, **k: (0, "weclaw is not running")  # type: ignore[method-assign]
        bridge._home_bridge_alive = lambda: True  # type: ignore[method-assign]
        bridge._terminate_home_processes = lambda: None  # type: ignore[method-assign]
        restarted: list[bool] = []
        bridge.start_bridge = lambda force=False: restarted.append(force) or {"code": 0}  # type: ignore[method-assign]
        with bridge._lock:
            bridge._bridge_log = (
                "[monitor] WARNING: WeChat session expired and cannot be auto-recovered."
            )

        status = bridge.status(with_svg=False)
        self.assertNotEqual(status["phase"], "expired", "多账号残留不应整体判为失效")
        self.assertFalse(status["expired"])
        self.assertNotIn("expired", events)
        for _ in range(100):
            if restarted:
                break
            time.sleep(0.02)
        self.assertEqual(restarted, [True], "清理残留账号后应强制重启桥接")
        self.assertEqual([p.name for p in bridge.account_files()], ["b-im-bot.json"])

    def test_autostart_recovers_expired_with_multiple_accounts(self):
        from ex_persona import webapp as webapp_module

        store.init_db()
        user_id = 900000003
        root = Path(_TMP.name) / "recover-home"
        accounts = root / ".weclaw" / "accounts"
        accounts.mkdir(parents=True, exist_ok=True)
        (accounts / "a-im-bot.json").write_text("{}", encoding="utf-8")
        (accounts / "b-im-bot.json").write_text("{}", encoding="utf-8")
        store.upsert_wechat_binding(
            user_id, bridge_token=f"tok-{user_id}", home_dir=str(root), phase="expired"
        )

        class StubBridge:
            def __init__(self) -> None:
                self.recovered = 0

            def account_files(self):
                return [accounts / "a-im-bot.json", accounts / "b-im-bot.json"]

            def recover(self):
                self.recovered += 1
                return {"code": 0}

            def maybe_autostart(self):
                raise AssertionError("残留账号未清理前不应直接自动拉起")

        stub = StubBridge()
        original_get = webapp_module.manager.get
        original_list = store.list_wechat_bindings
        webapp_module.manager.get = lambda uid, home_dir, token: stub  # type: ignore[method-assign]
        store.list_wechat_bindings = lambda: [store.get_wechat_binding(user_id)]  # type: ignore[method-assign]
        try:
            webapp_module._autostart_bridges()
        finally:
            webapp_module.manager.get = original_get  # type: ignore[method-assign]
            store.list_wechat_bindings = original_list  # type: ignore[method-assign]
        self.assertEqual(stub.recovered, 1)

    def test_signed_bridge_token(self):
        token = crypto.new_bridge_token(7)
        self.assertTrue(crypto.signed_bridge_token(token))
        self.assertTrue(crypto.verify_bridge_token(token))
        self.assertFalse(crypto.verify_bridge_token(token + "x"))
        self.assertFalse(crypto.verify_bridge_token("legacy-token"))

    def test_llm_error_classification(self):
        from ex_persona import llm

        class AuthenticationError(Exception):
            pass

        class RateLimitError(Exception):
            pass

        self.assertTrue(llm._classify(AuthenticationError()).auth)
        self.assertFalse(llm._retryable(AuthenticationError()))
        self.assertFalse(llm._classify(RateLimitError()).auth)
        self.assertTrue(llm._retryable(RateLimitError()))

    def test_llm_retries_transient_error(self):
        from ex_persona import llm
        from ex_persona.config import LLMConfig

        class APIConnectionError(Exception):
            pass

        attempts = {"n": 0}
        message = type("M", (), {"content": "嗨"})()
        response = type("R", (), {"choices": [type("C", (), {"message": message})()]})()

        class _Completions:
            def create(self, **payload):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise APIConnectionError("boom")
                return response

        client = type("Client", (), {"chat": type("Chat", (), {"completions": _Completions()})()})()
        original_make, original_sleep = llm.make_client, llm.time.sleep
        llm.make_client = lambda config: client
        llm.time.sleep = lambda seconds: None
        try:
            result = llm.chat(
                LLMConfig(api_key="k", base_url="x", model="m"),
                [{"role": "user", "content": "hi"}],
            )
        finally:
            llm.make_client, llm.time.sleep = original_make, original_sleep
        self.assertEqual(result, "嗨")
        self.assertEqual(attempts["n"], 2)

    def test_llm_auth_error_not_retried(self):
        from ex_persona import llm
        from ex_persona.config import LLMConfig

        class AuthenticationError(Exception):
            pass

        attempts = {"n": 0}

        class _Completions:
            def create(self, **payload):
                attempts["n"] += 1
                raise AuthenticationError("bad key")

        client = type("Client", (), {"chat": type("Chat", (), {"completions": _Completions()})()})()
        original_make = llm.make_client
        llm.make_client = lambda config: client
        try:
            with self.assertRaises(llm.LLMError) as caught:
                llm.chat(LLMConfig(api_key="k", base_url="x", model="m"), [{"role": "user", "content": "hi"}])
        finally:
            llm.make_client = original_make
        self.assertTrue(caught.exception.auth)
        self.assertEqual(attempts["n"], 1)

    def test_agent_falls_back_on_error(self):
        from ex_persona import agent as agent_module
        from ex_persona.config import LLMConfig

        root = Path(_TMP.name) / "fallback-persona"
        root.mkdir(parents=True, exist_ok=True)
        (root / "profile.json").write_text('{"name": "小鹿"}', encoding="utf-8")
        (root / "persona_card.json").write_text("{}", encoding="utf-8")
        (root / "style.json").write_text("{}", encoding="utf-8")
        (root / "SKILL.md").write_text("你是小鹿", encoding="utf-8")

        def boom(*args, **kwargs):
            raise agent_module.LLMError("挂了", auth=True)

        original = agent_module.chat
        agent_module.chat = boom
        try:
            instance = agent_module.PersonaAgent(
                root,
                config=LLMConfig(
                    api_key="k", base_url="x", model="m",
                    fallback_reply="哈哈，刚在忙，晚点回你。",
                ),
            )
            result = instance.reply("在吗")
        finally:
            agent_module.chat = original
        self.assertEqual(result, "哈哈，刚在忙，晚点回你。")
        self.assertTrue(instance.last_fallback)
        self.assertTrue(instance.last_error.auth)

    def test_system_prompt_includes_chat_style_rules(self):
        from ex_persona import agent as agent_module
        from ex_persona.config import LLMConfig

        root = Path(_TMP.name) / "chat-style-persona"
        root.mkdir(parents=True, exist_ok=True)
        (root / "profile.json").write_text('{"name": "小鹿"}', encoding="utf-8")
        (root / "persona_card.json").write_text("{}", encoding="utf-8")
        (root / "style.json").write_text("{}", encoding="utf-8")
        (root / "SKILL.md").write_text("你是小鹿", encoding="utf-8")
        instance = agent_module.PersonaAgent(
            root, config=LLMConfig(api_key="k", base_url="x", model="m")
        )

        system = instance.build_system("在吗")
        self.assertIn("反问", system, "系统提示必须要求人格适时反问")
        self.assertIn("每条单独占一行", system, "系统提示必须允许连发多条")
        # 稳定的人设与行为规则前置以命中前缀缓存，时间等易变内容后置。
        self.assertLess(system.index(agent_module.CHAT_STYLE_RULES), system.index("【当下时间】"))

    def test_safe_join_sanitizes_traversal(self):
        root = Path(_TMP.name) / "safe"
        root.mkdir(parents=True, exist_ok=True)
        escaped = workspace.safe_join(root, "../escape.txt")
        self.assertEqual(escaped.parent, root.resolve())
        self.assertEqual(escaped.name, "escape.txt")
        nested = workspace.safe_join(root, "../../etc/passwd")
        self.assertTrue(str(nested).startswith(str(root.resolve())))


class PlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        user = response.json()["user"]
        # 蒸馏需要消耗积分，测试账号统一预置足额积分。
        store.grant_credits(
            user["id"], 5000, reason="测试发放", actor="test", source="gift"
        )
        return user

    def test_wechat_status_keeps_expired_phase(self):
        from ex_persona import webapp as webapp_module

        with TestClient(app) as client:
            user = self._register(client, "wx-expired-user")
            home = str(Path(_TMP.name) / f"wx-{user['id']}")
            store.upsert_wechat_binding(
                user["id"], bridge_token=f"tok-{user['id']}", home_dir=home, phase="expired"
            )

            class StubBridge:
                def maybe_autostart(self):
                    raise AssertionError("expired 状态不应触发自动启动")

                def status(self, with_svg=True):
                    return {
                        "available": True,
                        "phase": "idle",
                        "running": False,
                        "expired": False,
                        "login_alive": False,
                    }

            original_get = webapp_module.manager.get
            original_bin = webapp_module.wechat.WECLAW_BIN
            webapp_module.manager.get = lambda *a, **k: StubBridge()
            webapp_module.wechat.WECLAW_BIN = "weclaw"
            try:
                response = client.get("/api/wechat/status")
            finally:
                webapp_module.manager.get = original_get
                webapp_module.wechat.WECLAW_BIN = original_bin
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["phase"], "expired")
            self.assertTrue(body["expired"])

    def test_me_requires_login(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_register_login_logout(self):
        with TestClient(app) as client:
            user = self._register(client, "alice")
            self.assertEqual(user["username"], "alice")
            me = client.get("/api/me").json()
            self.assertEqual(me["user"]["username"], "alice")
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/me").status_code, 401)
            relogin = client.post(
                "/api/auth/login", json={"username": "alice", "password": "Password123!"}
            )
            self.assertEqual(relogin.status_code, 200)

    def test_wrong_password(self):
        with TestClient(app) as client:
            self._register(client, "wrongpw")
            response = client.post(
                "/api/auth/login", json={"username": "wrongpw", "password": "nope-nope-nope"}
            )
            self.assertEqual(response.status_code, 401)

    def test_user_isolation(self):
        with TestClient(app) as alice:
            self._register(alice, "iso-alice")
            persona = alice.post("/api/personas", json={"name": "小鹿"}).json()["persona"]
            with TestClient(app) as bob:
                self._register(bob, "iso-bob")
                self.assertEqual(bob.get("/api/personas").json()["personas"], [])
                self.assertEqual(
                    bob.get(f"/api/skill?persona_id={persona['id']}").status_code, 404
                )
                # 越权激活他人人格
                self.assertEqual(
                    bob.post(f"/api/personas/{persona['id']}/activate").status_code, 404
                )

    def test_model_config_masked(self):
        with TestClient(app) as client:
            self._register(client, "model-user")
            response = client.put(
                "/api/model-config",
                json={"api_key": "sk-plain-secret-1234", "base_url": "https://x/v1", "model": "m"},
            )
            self.assertEqual(response.status_code, 200)
            response = client.get("/api/model-config")
            body = response.text
            self.assertNotIn("sk-plain-secret-1234", body)
            self.assertTrue(response.json()["has_key"])

    def test_model_config_rejects_internal_base_url(self):
        with TestClient(app) as client:
            self._register(client, "model-ssrf")
            for url in ("http://169.254.169.254/latest/meta-data", "http://localhost:8000",
                        "http://127.0.0.1/v1", "file:///etc/passwd"):
                response = client.put(
                    "/api/model-config",
                    json={"api_key": "sk-x", "base_url": url, "model": "m"},
                )
                self.assertEqual(response.status_code, 400, url)
            still_open = client.put(
                "/api/model-config",
                json={"api_key": "sk-x", "base_url": "https://api.deepseek.com/v1", "model": "m"},
            )
            self.assertEqual(still_open.status_code, 200, still_open.text)

    def test_build_config_uses_persona_provider(self):
        with TestClient(app) as client:
            user = self._register(client, "provider-user")
            client.put(
                "/api/model-config",
                json={"api_key": "sk-x", "base_url": "https://api.deepseek.com/v1",
                      "model": "deepseek-chat"},
            )
            kimi = build_config(user["id"], {"settings": '{"model": {"provider": "kimi"}}'})
            self.assertEqual(kimi.base_url, "https://api.moonshot.cn/v1")
            self.assertEqual(kimi.model, "moonshot-v1-8k")
            system = build_config(
                user["id"], {"settings": '{"model": {"provider": "system"}}'}
            )
            self.assertEqual(system.base_url, "https://api.deepseek.com/v1")
            self.assertEqual(system.model, "deepseek-chat")

    def test_route_chat_suppresses_silence_marker(self):
        with TestClient(app) as client:
            user = self._register(client, "silence-user")
            persona = client.post("/api/personas", json={"name": "林晚"}).json()["persona"]
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-silence",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )

            class _FakeConfig:
                ready = True

            class _FakeAgent:
                config = _FakeConfig()

                def reply(self, *args, **kwargs):
                    return "嗯\n[[SILENCE]]"

            from ex_persona import webapp as webapp_module

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            try:
                response = client.post(
                    "/v1/chat/completions/token-silence",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
            finally:
                webapp_module.get_agent = original
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "")

    def test_independent_session_keeps_own_history(self):
        with TestClient(app) as client:
            user = self._register(client, "indep-history")
            persona = client.post("/api/personas", json={"name": "小忆"}).json()["persona"]
            store.update_persona(
                user["id"],
                persona["id"],
                settings='{"advanced": {"independent_session": true},'
                ' "model": {"memory_mode": "deep"}}',
            )
            store.add_turn(user["id"], persona["id"], "user", "我昨天说想去哪来着", contact="c1")
            store.add_turn(
                user["id"], persona["id"], "assistant",
                "你这话说的 谁前言不搭后语了撒", contact="c1",
            )
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-indep",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            captured: dict = {}

            class _FakeConfig:
                ready = True

            class _FakeAgent:
                config = _FakeConfig()

                def reply(self, message, history=None, **kwargs):
                    captured["history"] = history or []
                    return "干嘛撒"

            from ex_persona import webapp as webapp_module

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            try:
                response = client.post(
                    "/v1/chat/completions/token-indep",
                    json={"user": "c1", "messages": [{"role": "user", "content": "你呀"}]},
                )
            finally:
                webapp_module.get_agent = original
            self.assertEqual(response.status_code, 200, response.text)
            history = captured["history"]
            self.assertTrue(history, "开启独立会话也不能丢弃当前对话的历史")
            self.assertEqual(history[0], {"role": "user", "content": "我昨天说想去哪来着"})
            self.assertEqual(
                history[-1],
                {"role": "assistant", "content": "你这话说的 谁前言不搭后语了撒"},
                "人格必须记得自己上一句说了什么",
            )
            self.assertNotIn({"role": "user", "content": "你呀"}, history)

    def test_wechat_login_disabled(self):
        with TestClient(app) as client:
            self._register(client, "wx-user")
            self.assertEqual(client.get("/api/auth/wechat/url").status_code, 404)
            self.assertFalse(client.get("/api/public-config").json()["wechat_login_enabled"])

    def test_bridge_token_routing(self):
        with TestClient(app) as client:
            user = self._register(client, "route-user")
            persona = client.post("/api/personas", json={"name": "回声"}).json()["persona"]
            binding = store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-route-user",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            self.assertEqual(binding["bridge_token"], "token-route-user")

            invalid = client.post("/v1/chat/completions/nope", json={"messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(invalid.status_code, 404)

            # 令牌有效但尚未启用平台模型：以正常回复告知用户，避免非 200 被静默丢弃
            valid = client.post(
                "/v1/chat/completions/token-route-user",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            self.assertEqual(valid.status_code, 200, valid.text)
            self.assertTrue(valid.json()["choices"][0]["message"]["content"])

    def test_route_chat_rejects_tampered_token(self):
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions/1.abc.deadbeef",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            self.assertEqual(response.status_code, 401)

    def test_rate_limit_helper(self):
        from ex_persona import webapp as webapp_module

        original = webapp_module.RATE_LIMIT_PER_MINUTE
        webapp_module.RATE_LIMIT_PER_MINUTE = 2
        webapp_module._rate_buckets.clear()
        try:
            self.assertFalse(webapp_module._rate_limited("rl-token"))
            self.assertFalse(webapp_module._rate_limited("rl-token"))
            self.assertTrue(webapp_module._rate_limited("rl-token"))
        finally:
            webapp_module.RATE_LIMIT_PER_MINUTE = original
            webapp_module._rate_buckets.clear()

    def test_route_chat_dedup_and_key_status(self):
        with TestClient(app) as client:
            user = self._register(client, "dedup-user")
            persona = client.post("/api/personas", json={"name": "小鹿"}).json()["persona"]
            client.put(
                "/api/model-config",
                json={"api_key": "sk-x", "base_url": "https://api.deepseek.com/v1",
                      "model": "deepseek-chat"},
            )
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-dedup",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            calls = {"n": 0}

            class _FakeConfig:
                ready = True

            class _FakeAgent:
                config = _FakeConfig()
                last_error = None
                last_fallback = False

                def reply(self, *args, **kwargs):
                    calls["n"] += 1
                    return "在呢"

            from ex_persona import webapp as webapp_module

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            try:
                first = client.post(
                    "/v1/chat/completions/token-dedup",
                    json={"messages": [{"role": "user", "content": "在吗在吗"}]},
                )
                second = client.post(
                    "/v1/chat/completions/token-dedup",
                    json={"messages": [{"role": "user", "content": "在吗在吗"}]},
                )
            finally:
                webapp_module.get_agent = original
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(first.json()["choices"][0]["message"]["content"], "在呢")
            turns = store.list_turns(user["id"], persona["id"], limit=10)
            self.assertEqual(len(turns), 2)
            self.assertEqual(client.get("/api/model-config").json()["key_status"], "ok")

    def test_route_chat_marks_invalid_key(self):
        with TestClient(app) as client:
            user = self._register(client, "badkey-user")
            persona = client.post("/api/personas", json={"name": "小鹿"}).json()["persona"]
            client.put(
                "/api/model-config",
                json={"api_key": "sk-bad", "base_url": "https://x/v1", "model": "m"},
            )
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-badkey",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            from ex_persona import webapp as webapp_module
            from ex_persona.llm import LLMError

            class _FakeConfig:
                ready = True

            class _FakeAgent:
                config = _FakeConfig()
                last_error = None
                last_fallback = False

                def reply(self, *args, **kwargs):
                    self.last_error = LLMError("模型鉴权失败", auth=True)
                    raise self.last_error

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            try:
                response = client.post(
                    "/v1/chat/completions/token-badkey",
                    json={"messages": [{"role": "user", "content": "你好呀呀"}]},
                )
            finally:
                webapp_module.get_agent = original
            # 模型鉴权失败时以正常回复告知用户，并仍标记 Key 失效
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["choices"][0]["message"]["content"])
            self.assertEqual(client.get("/api/model-config").json()["key_status"], "invalid")

    def test_landing_and_login_pages(self):
        with TestClient(app) as client:
            landing = client.get("/")
            self.assertEqual(landing.status_code, 200)
            self.assertIn("念念", landing.text)
            login = client.get("/login")
            self.assertEqual(login.status_code, 200)

    def test_create_distill_page(self):
        with TestClient(app) as client:
            self._register(client, "distill-page-user")
            page = client.get("/app/create/distill")
            self.assertEqual(page.status_code, 200)
            self.assertIn("AI 蒸馏", page.text)
            self.assertIn("开始蒸馏", page.text)

    def test_persona_detail_and_edit(self):
        with TestClient(app) as client:
            self._register(client, "detail-user")
            persona = client.post("/api/personas", json={"name": "阿离"}).json()["persona"]
            detail = client.get(f"/api/personas/{persona['id']}").json()
            self.assertEqual(detail["persona"]["name"], "阿离")
            self.assertIn("identity", detail["card"])
            self.assertIn("settings", detail)

            updated = client.patch(
                f"/api/personas/{persona['id']}",
                json={
                    "tag": "知己",
                    "card": {
                        "identity": {"birthday": "1998-02-03", "hometown": "苏州"},
                        "user": {"relationship": "老朋友"},
                        "memories": [{"title": "初见", "detail": "在书店", "time": "2019"}],
                    },
                },
            ).json()
            self.assertEqual(updated["persona"]["tag"], "知己")
            self.assertEqual(updated["card"]["identity"]["birthday"], "1998-02-03")
            self.assertEqual(updated["card"]["user"]["relationship"], "老朋友")
            self.assertEqual(updated["card"]["memories"][0]["title"], "初见")

    def test_create_from_preset(self):
        with TestClient(app) as client:
            self._register(client, "preset-user")
            presets = client.get("/api/presets").json()["presets"]
            self.assertTrue(presets)
            detail = client.post(
                "/api/personas/from-preset", json={"preset_id": presets[0]["id"]}
            ).json()
            self.assertEqual(detail["persona"]["status"], "ready")
            self.assertEqual(detail["card"]["identity"]["name"], presets[0]["name"])

    def test_persona_settings_and_reset(self):
        with TestClient(app) as client:
            self._register(client, "settings-user")
            persona = client.post("/api/personas", json={"name": "设置"}).json()["persona"]
            saved = client.put(
                f"/api/personas/{persona['id']}/settings",
                json={"settings": {"proactive": {"enabled": True, "interval_hours": 5}}},
            ).json()["settings"]
            self.assertTrue(saved["proactive"]["enabled"])
            self.assertEqual(saved["proactive"]["interval_hours"], 5)
            self.assertIn("model", saved)

            reset = client.post(f"/api/personas/{persona['id']}/reset").json()
            self.assertTrue(reset["ok"])

    def test_sticker_upload_and_public_access(self):
        with TestClient(app) as client:
            user = self._register(client, "sticker-user")
            persona = client.post("/api/personas", json={"name": "表情"}).json()["persona"]
            uploaded = client.post(
                f"/api/personas/{persona['id']}/stickers",
                files={"file": ("wave.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            ).json()
            sticker = uploaded["sticker"]
            self.assertEqual(len(uploaded["stickers"]), 1)

            self.assertEqual(client.get(f"/public/sticker/{sticker['id']}").status_code, 403)
            binding = store.upsert_wechat_binding(
                user["id"],
                bridge_token="sticker-token",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            ok = client.get(f"/public/sticker/{sticker['id']}?token={binding['bridge_token']}")
            self.assertEqual(ok.status_code, 200)

            removed = client.delete(
                f"/api/personas/{persona['id']}/stickers/{sticker['id']}"
            ).json()
            self.assertEqual(removed["stickers"], [])

    def test_wechat_media_forwarding(self):
        with TestClient(app) as client:
            user = self._register(client, "media-user")
            persona = client.post("/api/personas", json={"name": "收图"}).json()["persona"]
            binding = store.upsert_wechat_binding(
                user["id"],
                bridge_token="media-token",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            token = binding["bridge_token"]

            self.assertEqual(
                client.post(
                    "/api/wechat/media/missing-token",
                    files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                ).status_code,
                404,
            )

            response = client.post(
                f"/api/wechat/media/{token}",
                headers={"X-WeChat-From": "peer9@im.wechat"},
                files={"file": ("gift.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["persona_id"], persona["id"])
            self.assertEqual(body["sticker"]["name"], "gift.png")

            stickers = client.get(f"/api/personas/{persona['id']}/stickers").json()["stickers"]
            self.assertEqual(len(stickers), 1)
            self.assertEqual(stickers[0]["id"], body["sticker"]["id"])

            saved = client.get(f"/api/personas/{persona['id']}/channel").json()
            self.assertEqual(saved["wechat"]["contact"], "peer9@im.wechat")

    def test_channel_contact(self):
        with TestClient(app) as client:
            self._register(client, "channel-user")
            persona = client.post("/api/personas", json={"name": "渠道"}).json()["persona"]
            channel = client.get(f"/api/personas/{persona['id']}/channel").json()
            self.assertIn("wechat", channel)
            self.assertIn("qq", channel)
            self.assertIn("available", channel["wechat"])
            saved = client.put(
                f"/api/personas/{persona['id']}/channel/contact",
                json={"contact": "user123@im.wechat"},
            ).json()
            self.assertEqual(saved["contact"], "user123@im.wechat")

    def _wait_task(self, client, task_id, attempts=40):
        import time
        task = {}
        for _ in range(attempts):
            task = client.get(f"/api/tasks/{task_id}").json()
            if task.get("status") in ("done", "error"):
                return task
            time.sleep(0.05)
        return task

    def test_create_and_distill_from_form(self):
        with TestClient(app) as client:
            self._register(client, "form-distill-user")
            response = client.post(
                "/api/personas/distill",
                data={
                    "name": "小满",
                    "gender": "女",
                    "personality": "温柔,细心",
                    "catchphrases": "唔,好呀",
                    "style": "喜欢用短句",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            task = self._wait_task(client, body["task_id"])
            self.assertEqual(task["status"], "done", task)
            detail = client.get(f"/api/personas/{body['persona']['id']}").json()
            self.assertEqual(detail["card"]["identity"]["gender"], "女")
            self.assertEqual(detail["card"]["personality"], ["温柔", "细心"])
            self.assertEqual(detail["persona"]["status"], "ready")

    def test_distill_client_id_is_idempotent(self):
        with TestClient(app) as client:
            self._register(client, "idem-distill-user")
            payload = {"name": "小满", "gender": "女", "client_id": "attempt-abc"}
            first = client.post("/api/personas/distill", data=payload).json()
            self.assertFalse(first["reused"])
            self._wait_task(client, first["task_id"])

            second = client.post("/api/personas/distill", data=payload).json()
            self.assertTrue(second["reused"])
            self.assertEqual(second["persona"]["id"], first["persona"]["id"])

            third = client.post(
                "/api/personas/distill", data={**payload, "client_id": "attempt-xyz"}
            ).json()
            self.assertFalse(third["reused"])
            self.assertNotEqual(third["persona"]["id"], first["persona"]["id"])

            names = [p["name"] for p in client.get("/api/personas").json()["personas"]]
            self.assertEqual(names.count("小满"), 2)

    def test_distill_client_id_retry_reuses_persona_after_error(self):
        with TestClient(app) as client:
            self._register(client, "idem-retry-user")
            data = {"name": "小满", "client_id": "retry-1"}
            first = client.post("/api/personas/distill", data=data).json()
            self._wait_task(client, first["task_id"])
            store.update_task(first["task_id"], status="error", error="boom")

            again = client.post("/api/personas/distill", data=data).json()
            self.assertTrue(again["reused"])
            self.assertEqual(again["persona"]["id"], first["persona"]["id"])
            self.assertNotEqual(again["task_id"], first["task_id"])
            personas = client.get("/api/personas").json()["personas"]
            self.assertEqual(len(personas), 1)

    def test_redistill_with_material(self):
        with TestClient(app) as client:
            self._register(client, "redistill-user")
            chat = (
                "2023-05-01 21:03:00 小鹿\n你好呀\n"
                "2023-05-01 21:04:00 我\n在的\n"
                "2023-05-01 21:05:00 小鹿\n今天有点想你\n"
                "2023-05-01 21:06:00 我\n我也是\n"
                "2023-05-01 21:07:00 小鹿\n那明天见啦\n"
            )
            response = client.post(
                "/api/personas/distill",
                data={"name": "小鹿", "target": "小鹿"},
                files=[("files", ("chat.txt", chat.encode("utf-8"), "text/plain"))],
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            task = self._wait_task(client, body["task_id"])
            self.assertEqual(task["status"], "done", task)
            detail = client.get(f"/api/personas/{body['persona']['id']}").json()
            self.assertGreater(detail["style"]["message_count"], 0)
            self.assertTrue(detail["card"]["soul"]["catchphrases"])

    def test_upload_and_paste_share_one_batch(self):
        from pathlib import Path

        from ex_persona import webapp as webapp_module

        with TestClient(app) as client:
            user = self._register(client, "material-batch-user")
            pasted = client.post("/api/paste", json={"text": "2023-05-01 21:03:00 小鹿\n你好呀"})
            uploaded = client.post(
                "/api/upload",
                files={"files": ("chat.txt", b"2023-05-01 21:04:00 \xe6\x88\x91\n\xe5\x9c\xa8\xe7\x9a\x84", "text/plain")},
            )
            self.assertEqual(pasted.status_code, 200, pasted.text)
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            batch = Path(webapp_module._last_batch[user["id"]])
            names = sorted(item.name for item in batch.iterdir())
            # 上传和粘贴累积到同一批，而不是互相覆盖。
            self.assertIn("pasted.txt", names)
            self.assertIn("chat.txt", names)

    def test_channel_weclaw_bridge_info(self):
        with TestClient(app) as client:
            user = self._register(client, "weclaw-user")
            persona = client.post("/api/personas", json={"name": "机器人"}).json()["persona"]
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="weclaw-token",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            channel = client.get(f"/api/personas/{persona['id']}/channel").json()
            self.assertEqual(channel["wechat"]["token"], "weclaw-token")
            self.assertEqual(channel["wechat"]["endpoint"], "/v1/chat/completions/weclaw-token")

    def test_delete_persona(self):
        with TestClient(app) as client:
            self._register(client, "delete-persona-user")
            persona = client.post("/api/personas", json={"name": "临时"}).json()["persona"]
            self.assertEqual(client.delete(f"/api/personas/{persona['id']}").json()["ok"], True)
            self.assertEqual(client.get(f"/api/personas/{persona['id']}").status_code, 404)

    def test_task_isolation(self):
        with TestClient(app) as client:
            self._register(client, "task-a")
            persona = client.post("/api/personas", json={"name": "任务"}).json()["persona"]
            task_id = client.post(
                f"/api/personas/{persona['id']}/redistill", json={"use_llm": False}
            ).json()["task_id"]
            self._wait_task(client, task_id)
        with TestClient(app) as other:
            other.post(
                "/api/auth/register", json={"username": "task-b", "password": "Password123!"}
            )
            self.assertEqual(other.get(f"/api/tasks/{task_id}").status_code, 404)


class MemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def _register(self, client, username):
        response = client.post(
            "/api/auth/register", json={"username": username, "password": "Password123!"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["user"]

    def test_turns_isolated_by_contact(self):
        with TestClient(app) as client:
            user = self._register(client, "turn-iso")
            persona = client.post("/api/personas", json={"name": "记忆"}).json()["persona"]
            store.add_turn(user["id"], persona["id"], "user", "A 的话", contact="contact-a")
            store.add_turn(user["id"], persona["id"], "assistant", "A 的回复", contact="contact-a")
            store.add_turn(user["id"], persona["id"], "user", "B 的话", contact="contact-b")

            turns_a = store.list_turns(user["id"], persona["id"], contact="contact-a")
            self.assertEqual([t["content"] for t in turns_a], ["A 的话", "A 的回复"])
            turns_b = store.list_turns(user["id"], persona["id"], contact="contact-b")
            self.assertEqual([t["content"] for t in turns_b], ["B 的话"])
            self.assertEqual(store.count_turns(user["id"], persona["id"], "contact-a"), 2)
            all_turns = store.list_turns(user["id"], persona["id"])
            self.assertEqual(len(all_turns), 3)

    def test_memory_crud_and_isolation(self):
        with TestClient(app) as client:
            user = self._register(client, "memory-crud")
            persona = client.post("/api/personas", json={"name": "记忆"}).json()["persona"]
            store.add_memory(user["id"], persona["id"], "c1", "fact", "喜欢咖啡")
            store.add_memory(user["id"], persona["id"], "c1", "preference", "讨厌加班")
            store.add_memory(user["id"], persona["id"], "c2", "fact", "养了一只猫")

            self.assertEqual(store.count_memories(user["id"], persona["id"], "c1"), 2)
            self.assertEqual(store.count_memories(user["id"], persona["id"], "c2"), 1)
            self.assertEqual(store.count_memories(user["id"], persona["id"]), 3)

            items = store.list_memories(user["id"], persona["id"], "c1")
            self.assertEqual(len(items), 2)
            self.assertTrue(store.delete_memory(user["id"], persona["id"], items[0]["id"]))
            self.assertEqual(store.count_memories(user["id"], persona["id"], "c1"), 1)
            store.clear_memories(user["id"], persona["id"], "c1")
            self.assertEqual(store.count_memories(user["id"], persona["id"], "c1"), 0)
            self.assertEqual(store.count_memories(user["id"], persona["id"], "c2"), 1)

    def test_memory_update_endpoint(self):
        with TestClient(app) as client:
            user = self._register(client, "memory-edit")
            persona = client.post("/api/personas", json={"name": "记忆"}).json()["persona"]
            store.add_memory(user["id"], persona["id"], "c1", "fact", "喜欢咖啡")
            memory = store.list_memories(user["id"], persona["id"], "c1")[0]

            response = client.put(
                f"/api/personas/{persona['id']}/memories/{memory['id']}",
                json={"content": "其实更喜欢抹茶", "kind": "preference"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            updated = store.list_memories(user["id"], persona["id"], "c1")[0]
            self.assertEqual(updated["content"], "其实更喜欢抹茶")
            self.assertEqual(updated["kind"], "preference")

            blank = client.put(
                f"/api/personas/{persona['id']}/memories/{memory['id']}",
                json={"content": "   "},
            )
            self.assertEqual(blank.status_code, 400)

            missing = client.put(
                f"/api/personas/{persona['id']}/memories/99999999",
                json={"content": "不存在"},
            )
            self.assertEqual(missing.status_code, 404)

    def test_memory_parse_and_recall(self):
        from ex_persona import memories as memory_module

        parsed = memory_module.parse(
            '{"memories": [{"kind": "fact", "content": " 喜欢咖啡。 "},'
            ' {"kind": "unknown", "content": "讨厌加班"},'
            ' {"kind": "fact", "content": ""}]}'
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0], {"kind": "fact", "content": "喜欢咖啡"})
        self.assertEqual(parsed[1]["kind"], "fact")

        self.assertEqual(memory_module.parse("not json"), [])
        fenced = memory_module.parse('```json\n[{"kind":"event","content":"一起去海边"}]\n```')
        self.assertEqual(fenced, [{"kind": "event", "content": "一起去海边"}])

        items = [
            {"kind": "fact", "content": "喜欢咖啡"},
            {"kind": "fact", "content": "养了一只叫团子的猫"},
        ]
        hits = memory_module.recall(items, "团子最近怎么样")
        self.assertEqual(len(hits), 1)
        self.assertIn("团子", hits[0]["content"])
        self.assertEqual(memory_module.recall(items, "zzzqqq"), [])
        block = memory_module.format_block(hits, "小鹿")
        self.assertIn("长期记忆", block)
        self.assertIn("团子", block)

    def test_route_chat_isolates_contact_memory(self):
        with TestClient(app) as client:
            user = self._register(client, "memory-route")
            persona = client.post(
                "/api/personas",
                json={"name": "林晚", "settings": '{"model": {"memory_extract_every": 50}}'},
            ).json()["persona"]
            store.upsert_wechat_binding(
                user["id"],
                bridge_token="token-memory-route",
                home_dir=str(workspace.home_dir(user["id"])),
                persona_id=persona["id"],
            )
            store.add_memory(user["id"], persona["id"], "contact-a", "fact", "她喜欢咖啡")

            seen: list[str] = []

            class _FakeConfig:
                ready = True

            class _FakeAgent:
                config = _FakeConfig()

                def reply(self, message, history=None, temperature=None, extra_context="", **kwargs):
                    seen.append(extra_context)
                    return "好呀"

            from ex_persona import webapp as webapp_module

            original = webapp_module.get_agent
            webapp_module.get_agent = lambda uid, p: _FakeAgent()
            try:
                client.post(
                    "/v1/chat/completions/token-memory-route",
                    json={"user": "contact-a", "messages": [{"role": "user", "content": "想喝咖啡"}]},
                )
                client.post(
                    "/v1/chat/completions/token-memory-route",
                    json={"user": "contact-b", "messages": [{"role": "user", "content": "想喝咖啡"}]},
                )
            finally:
                webapp_module.get_agent = original

            self.assertEqual(len(seen), 2)
            self.assertIn("咖啡", seen[0])
            self.assertNotIn("咖啡", seen[1])

            turns_a = store.list_turns(user["id"], persona["id"], contact="contact-a")
            turns_b = store.list_turns(user["id"], persona["id"], contact="contact-b")
            self.assertEqual([t["content"] for t in turns_a], ["想喝咖啡", "好呀"])
            self.assertEqual([t["content"] for t in turns_b], ["想喝咖啡", "好呀"])

    def test_memory_api(self):
        with TestClient(app) as client:
            user = self._register(client, "memory-api")
            persona = client.post("/api/personas", json={"name": "记忆"}).json()["persona"]
            store.touch_contact(user["id"], persona["id"], "wx-1", "妈妈")
            memory = store.add_memory(user["id"], persona["id"], "wx-1", "fact", "喜欢喝美式")

            listing = client.get(f"/api/personas/{persona['id']}/memories").json()
            self.assertTrue(listing["enabled"])
            self.assertEqual(listing["selected"], "wx-1")
            self.assertEqual(len(listing["memories"]), 1)
            self.assertEqual(listing["contacts"][0]["display_name"], "妈妈")

            renamed = client.patch(
                f"/api/personas/{persona['id']}/contacts/wx-1", json={"name": "母亲"}
            )
            self.assertEqual(renamed.status_code, 200)
            self.assertEqual(store.get_contact(user["id"], persona["id"], "wx-1")["display_name"], "母亲")

            self.assertEqual(
                client.delete(f"/api/personas/{persona['id']}/memories/{memory['id']}").status_code,
                200,
            )
            self.assertEqual(store.count_memories(user["id"], persona["id"]), 0)

    def test_feedback_submit(self):
        with TestClient(app) as client:
            user = self._register(client, "feedback-api")
            persona = client.post("/api/personas", json={"name": "反馈"}).json()["persona"]

            empty = client.post(f"/api/personas/{persona['id']}/feedback", json={"content": "  "})
            self.assertEqual(empty.status_code, 400)

            response = client.post(
                f"/api/personas/{persona['id']}/feedback", json={"content": "希望支持 QQ 渠道"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["ok"])

            items = store.list_feedback(user["id"])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["content"], "希望支持 QQ 渠道")
            self.assertEqual(items[0]["persona_id"], persona["id"])


if __name__ == "__main__":
    unittest.main()
