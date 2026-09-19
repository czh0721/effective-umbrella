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
        for marker in ("语音 MiniMax Key", "TTS 模型名", "音色克隆成本", "语音回复成本", "启用语音服务"):
            self.assertIn(marker, text)

    def test_agent_voice_panel_present(self):
        text = (WEB_ROOT / "agent.html").read_text(encoding="utf-8")
        for marker in ("克隆音色", "试听", "同意", "预设音色", "playVoicePreview"):
            self.assertIn(marker, text)

    def test_voice_module_uses_minimax_endpoints(self):
        source = (
            Path(__file__).resolve().parent.parent / "ex_persona" / "voice.py"
        ).read_text(encoding="utf-8")
        self.assertIn("t2a_v2", source)
        self.assertIn("voice_clone", source)
        self.assertIn("SILK", source)


if __name__ == "__main__":
    unittest.main()
