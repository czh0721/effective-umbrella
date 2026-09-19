"""前后端契约测试：前端引用的每个接口路径都必须能在后端路由中匹配到。

前端页面通过字符串拼接调用后端，路径一旦写错只能等用户点到才暴露。这里静态
提取 ``web/`` 下所有以 /api、/admin、/v1、/public、/health 开头的字符串字面量，
把动态片段统一成 ``{}`` 后与 FastAPI 路由模板逐一比对，防止接口漂移。
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["PERSONA_DATA_DIR"] = _TMP.name
os.environ["PERSONA_SECRET_KEY"] = "unit-test-secret"
os.environ["WECLAW_BIN"] = ""
os.environ.setdefault("PERSONA_REGISTER_MAX", "1000")

from ex_persona.webapp import app  # noqa: E402

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PREFIXES = ("/api/", "/admin", "/v1/", "/public/", "/health")
PATH_RE = re.compile(r"""["'`](/(?:api|admin|v1|public|health)[A-Za-z0-9_\-/{}.]*)["'`]""")
PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")


def frontend_paths() -> set[str]:
    paths: set[str] = set()
    # index.html 是无路由的孤儿页（死代码保留），其接口调用不参与契约校验。
    files = [
        path for path in WEB_ROOT.rglob("*.html") if path.name != "index.html"
    ] + [WEB_ROOT / "static" / "app.js"]
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in PATH_RE.finditer(text):
            raw = PLACEHOLDER_RE.sub("{}", match.group(1)).split("?")[0]
            if raw.endswith("/") or raw.endswith("{}"):
                # 前缀拼接（如 "/api/personas/" + id）无法静态断言，跳过。
                continue
            paths.add(raw)
    return paths


def backend_templates() -> set[str]:
    templates: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or not path.startswith(PREFIXES):
            continue
        templates.add(re.sub(r"\{[^}]*\}", "{}", path))
    return templates


class FrontendBackendContractTest(unittest.TestCase):
    def test_every_frontend_path_has_a_route(self):
        templates = backend_templates()
        missing = []
        for path in sorted(frontend_paths()):
            if path in templates:
                continue
            # 仅作为前缀出现（如 path.startsWith("/api/auth")）的字符串不是接口调用。
            if any(template.startswith(path + "/") for template in templates):
                continue
            # 允许带路径参数的具体调用匹配到参数化模板，例如
            # /api/personas/8/memories/11 -> /api/personas/{}/memories/{}
            if any(self._matches(path, template) for template in templates):
                continue
            missing.append(path)
        self.assertEqual(missing, [], f"前端调用了后端不存在的接口：{missing}")

    @staticmethod
    def _matches(path: str, template: str) -> bool:
        path_parts = [part for part in path.strip("/").split("/")]
        tpl_parts = [part for part in template.strip("/").split("/")]
        if len(path_parts) != len(tpl_parts):
            return False
        for actual, expected in zip(path_parts, tpl_parts, strict=True):
            if expected == "{}":
                continue
            if actual != expected:
                return False
        return True


class VoiceUiContractTest(unittest.TestCase):
    def test_admin_voice_settings_present(self):
        text = (WEB_ROOT / "admin.html").read_text(encoding="utf-8")
        for marker in ("语音提供商", "语音 MiniMax 密钥", "语音合成模型", "豆包应用 ID",
                       "豆包访问令牌", "音色克隆扣积分", "语音回复扣积分", "启用语音服务"):
            self.assertIn(marker, text)

    def test_agent_voice_panel_present(self):
        text = (WEB_ROOT / "agent.html").read_text(encoding="utf-8")
        for marker in ("克隆音色", "试听", "同意", "预设音色", "playVoicePreview",
                       "上传本地音频", "voice/samples", "自动收集微信语音", "收集进度"):
            self.assertIn(marker, text)

    def test_voice_module_uses_minimax_endpoints(self):
        source = (
            Path(__file__).resolve().parent.parent / "ex_persona" / "voice.py"
        ).read_text(encoding="utf-8")
        self.assertIn("t2a_v2", source)
        self.assertIn("voice_clone", source)
        self.assertIn("SILK", source)

    def test_voice_module_uses_doubao_endpoints(self):
        source = (
            Path(__file__).resolve().parent.parent / "ex_persona" / "voice.py"
        ).read_text(encoding="utf-8")
        self.assertIn("openspeech.bytedance.com", source)
        self.assertIn("tts/unidirectional", source)
        self.assertIn("tts/voice_clone", source)
        self.assertIn("seed-icl-2.0", source)

    def test_admin_platform_exposes_provider_switch(self):
        source = (
            Path(__file__).resolve().parent.parent / "ex_persona" / "webapp.py"
        ).read_text(encoding="utf-8")
        self.assertIn("voice_provider", source)
        self.assertIn("doubao_access_token", source)

    def test_voice_sample_upload_endpoint_present(self):
        source = (
            Path(__file__).resolve().parent.parent / "ex_persona" / "webapp.py"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/personas/{persona_id}/voice/samples", source)
        self.assertIn("sample_upload", source)

    def test_agent_voice_module_disabled_by_default(self):
        text = (WEB_ROOT / "agent.html").read_text(encoding="utf-8")
        self.assertIn("const VOICE_MODULE_ENABLED = false;", text)
        self.assertIn('VOICE_MODULE_ENABLED ? row("voice"', text)
        self.assertIn('VOICE_MODULE_ENABLED ? switchRow("允许语音回复"', text)
        self.assertIn("VOICE_MODULE_ENABLED ? body.querySelector(\"#fVoice\").checked", text)


class AdminConsoleStructureTest(unittest.TestCase):
    def setUp(self):
        self.text = (WEB_ROOT / "admin.html").read_text(encoding="utf-8")

    def test_nav_split_into_five_zones_in_order(self):
        nav = self.text.split('id="sideNav"', 1)[1].split("</nav>", 1)[0]
        labels = re.findall(r'<div class="label">([^<]+)</div>', nav)
        self.assertEqual(labels, ["概览", "用户运营", "商业化", "系统运维", "权限管理"])
        tabs = re.findall(r'data-tab="([a-z]+)"', nav)
        self.assertEqual(
            tabs,
            ["overview", "users", "content", "reports", "ops",
             "orders", "credits", "system", "audit", "admins"],
        )

    def test_section_description_rendered(self):
        self.assertIn('id="pageDesc"', self.text)
        self.assertIn('const desc = meta.desc || ""', self.text)
        self.assertIn('$("pageDesc").style.display = desc ? "" : "none"', self.text)
        for desc in ("一屏速览", "重置密码", "触达效果", "兑换码", "登录锁定"):
            self.assertIn(desc, self.text)
        # 操作审计分区不需要说明文案
        audit_line = [line for line in self.text.splitlines() if 'key: "audit"' in line]
        self.assertEqual(len(audit_line), 1)
        self.assertNotIn("desc:", audit_line[0])

    def test_status_terms_are_chinese(self):
        for marker in ("运行中", "空闲", "异常", "已中断", "信息", "警告", "严重"):
            self.assertIn(marker, self.text)
        for stale in ("TTS 模型名", "豆包资源 ID", "音色克隆成本", "语音回复成本"):
            self.assertNotIn(stale, self.text)

    def test_voice_readiness_chip_present(self):
        for marker in ("语音已就绪 · ", "语音未启用 · ", "语音缺少 "):
            self.assertIn(marker, self.text)
        self.assertIn("const voiceCredsReady", self.text)
        self.assertIn("platform.has_doubao_key", self.text)
        self.assertIn("platform.has_doubao_token", self.text)
        self.assertIn('<span class="state-line">${voiceChip}</span>', self.text)


if __name__ == "__main__":
    unittest.main()
