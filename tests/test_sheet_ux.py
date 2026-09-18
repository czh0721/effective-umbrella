"""人设页浮层（bottom sheet）体验回归测试。

曾经的问题：从「记忆与会话 → 长期记忆」打开浮层后，内容往下滑很卡，滑到底部
偶发加载不出来，而且浮层顶部没有返回按钮。根因有两处：

1. 浮层遮罩用 ``backdrop-filter`` 模糊背景，而背景里的环星 canvas 与极光
   ``body::before`` 一直在动，浏览器每帧都要重新取景模糊；多层浮层还会叠加多层
   全屏模糊，滚动时明显掉帧。
2. 浮层把整个 ``.modal`` 当成滚动容器，标题和底部按钮一起被滚走；异步加载失败时
   骨架屏会永远停在那里，用户也找不到返回入口。

这里用静态契约把修复钉死，避免以后改 CSS/JS 时回归。
"""

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
APP_JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")
APP_CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
AGENT_HTML = (WEB / "agent.html").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """取出某个选择器对应的规则体（不处理嵌套，够用即可）。"""
    match = re.search(
        re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}", css
    )
    if not match:
        raise AssertionError(f"CSS 里找不到规则：{selector}")
    return match.group(1)


class SheetBackButtonTest(unittest.TestCase):
    def test_sheet_has_back_button(self):
        self.assertIn('id="sheetBack"', APP_JS)
        self.assertIn('class="icon-btn sheet-back"', APP_JS)
        self.assertIn('aria-label="返回"', APP_JS)

    def test_back_button_skips_autofocus(self):
        # 打开浮层时不应该把焦点自动落在返回按钮上（会亮出一个无意义的 focus ring）。
        self.assertIn("data-skip-focus", APP_JS)
        self.assertIn("el.hasAttribute(\"data-skip-focus\")", APP_JS)

    def test_back_button_closes_sheet(self):
        self.assertRegex(
            APP_JS,
            r'querySelector\("#sheetBack"\)\.onclick\s*=\s*\(\)\s*=>\s*closeModal\(mask\)',
        )

    def test_header_sits_outside_scroll_body(self):
        # 标题栏是模态框的直接子节点，滚动容器只包含正文。
        self.assertIn('class="sheet-head"', APP_JS)
        self.assertRegex(APP_JS, r'<div class="sheet-head">[\s\S]*?</div>\s*<div class="modal-body')


class SheetScrollLayoutTest(unittest.TestCase):
    def test_modal_is_flex_column_and_clips(self):
        body = _rule(APP_CSS, ".modal-mask.sheet .modal")
        self.assertIn("display: flex", body)
        self.assertIn("flex-direction: column", body)
        self.assertIn("overflow: hidden", body)

    def test_body_is_the_scroll_container(self):
        body = _rule(APP_CSS, ".modal-mask.sheet .modal-body")
        self.assertIn("overflow-y: auto", body)
        self.assertIn("min-height: 0", body)
        self.assertIn("-webkit-overflow-scrolling: touch", body)
        self.assertIn("overscroll-behavior: contain", body)

    def test_footer_does_not_scroll_away(self):
        body = _rule(APP_CSS, ".modal-mask.sheet .modal > .btnrow")
        self.assertIn("flex: none", body)


class AmbientPauseTest(unittest.TestCase):
    def test_sync_scroll_lock_pauses_ambient(self):
        self.assertIn("setAmbientPaused(masks.length > 0)", APP_JS)

    def test_ambient_paused_toggles_root_class(self):
        self.assertIn("document.documentElement.classList.toggle(\"ambient-paused\"", APP_JS)

    def test_starfield_frame_skips_when_paused(self):
        self.assertRegex(APP_JS, r"if \(ambientPaused\) \{ last = 0; return; \}")

    def test_css_pauses_aurora_drift(self):
        body = _rule(APP_CSS, "html.ambient-paused body::before")
        self.assertIn("animation-play-state: paused", body)

    def test_underlying_mask_drops_backdrop_blur(self):
        self.assertIn("classList.toggle(\"under\"", APP_JS)
        body = _rule(APP_CSS, ".modal-mask.under")
        self.assertIn("backdrop-filter: none", body)


class MemorySheetResilienceTest(unittest.TestCase):
    def test_memory_sheets_surface_load_errors(self):
        self.assertIn("function sheetLoadFailed", AGENT_HTML)
        self.assertIn("sheetLoadFailed(box, error.message, render)", AGENT_HTML)
        self.assertIn("sheetLoadFailed(host, error.message, draw)", AGENT_HTML)

    def test_memory_sheets_offer_retry(self):
        self.assertIn('id="sheetRetry"', AGENT_HTML)
        self.assertIn("button.onclick = retry", AGENT_HTML)


if __name__ == "__main__":
    unittest.main()
