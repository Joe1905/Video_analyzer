#!/usr/bin/env python3
"""Static contracts for the shared native HTML/CSS/JS UI shell."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
STATIC_DIR = SCRIPTS_DIR / "static"
UI_CSS = STATIC_DIR / "assets" / "ui-system.css"
UI_JS = STATIC_DIR / "assets" / "ui-system.js"
TEMPLATES = (
    STATIC_DIR / "amazon.html",
    STATIC_DIR / "chat.html",
    STATIC_DIR / "lan_chat.html",
    STATIC_DIR / "metrics.html",
    STATIC_DIR / "proxy.html",
    STATIC_DIR / "report.html",
    STATIC_DIR / "report_player.html",
    STATIC_DIR / "shop.html",
    STATIC_DIR / "tool.html",
    SCRIPTS_DIR / "web_index.html",
)


class ShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.shell_paths: list[tuple[str, ...]] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        self.stack.append((tag, classes))
        if attr_map.get("id"):
            self.ids.append(str(attr_map["id"]))
        if classes.intersection({"ui-app", "ui-frame", "ui-header", "ui-main"}):
            self.shell_paths.append(tuple(next(iter(item[1].intersection({"ui-app", "ui-frame", "ui-header", "ui-main"})), "") for item in self.stack))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


class UIContractTest(unittest.TestCase):
    def test_every_template_uses_the_shared_shell(self) -> None:
        for path in TEMPLATES:
            name = path.name
            with self.subTest(template=name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count("<!-- UI_APP_NAV -->"), 1)
                parser = ShellParser()
                parser.feed(text)
                for contract_class in ("ui-app", "ui-frame", "ui-header", "ui-main"):
                    self.assertEqual(
                        sum(path[-1] == contract_class for path in parser.shell_paths),
                        1,
                        f"{name} must contain exactly one .{contract_class}",
                    )
                self.assertTrue(
                    any(path[-2:] == ("ui-app", "ui-frame") for path in parser.shell_paths),
                    f"{name} .ui-frame must be inside .ui-app",
                )
                self.assertTrue(
                    any(path[-3:] == ("ui-app", "ui-frame", "ui-header") for path in parser.shell_paths),
                    f"{name} .ui-header must be inside .ui-frame",
                )
                self.assertTrue(
                    any(path[-3:] == ("ui-app", "ui-frame", "ui-main") for path in parser.shell_paths),
                    f"{name} .ui-main must be inside .ui-frame",
                )
                duplicates = {item for item in parser.ids if parser.ids.count(item) > 1}
                self.assertFalse(duplicates, f"{name} has duplicate IDs: {sorted(duplicates)}")

    def test_common_css_is_layered_and_override_free(self) -> None:
        css = UI_CSS.read_text(encoding="utf-8")
        self.assertIn(
            "@layer legacy, reset, tokens, base, layout, components, pages, utilities;",
            css,
        )
        self.assertNotIn("!important", css)
        for selector in (
            ".ui-app",
            ".ui-nav",
            ".ui-nav__brand",
            ".ui-nav__icon",
            ".ui-frame",
            ".ui-header",
            ".ui-main",
            ".ui-collapse",
            ".ui-dialog",
            ".ui-badge",
        ):
            self.assertIn(selector, css)
        self.assertRegex(css, r"grid-template-columns:\s*var\(--ui-nav-width\)")
        self.assertIn('html[data-nav="expanded"] .ui-nav__item', css)
        self.assertIn('html[data-nav="expanded"] .ui-nav__label', css)
        self.assertIn("grid-template-rows: 0fr", css)
        self.assertIn("grid-template-rows: 1fr", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)

    def test_shared_behavior_preserves_accessibility_state(self) -> None:
        script = UI_JS.read_text(encoding="utf-8")
        for behavior in (
            'body.dataset.nav',
            'document.cookie',
            'aria-expanded',
            'aria-hidden',
            'MutationObserver',
            'ui_state',
        ):
            self.assertIn(behavior, script)
        self.assertFalse(re.search(r"\beval\s*\(", script))

    def test_runtime_uses_placeholders_and_external_assets(self) -> None:
        app = (SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8")
        self.assertIn('html.replace("<!-- UI_APP_NAV -->", nav, 1)', app)
        self.assertIn('href="/assets/ui-system.css?v=', app)
        self.assertIn('src="/assets/ui-system.js?v=', app)
        self.assertIn('cache_control="no-cache, no-store, must-revalidate"', app)
        self.assertIn('CHAT_PROVIDER_ICONS', app)
        self.assertIn('"amazon": "\\u5356\\u5bb6\\u7cbe\\u7075"', app)
        self.assertIn('class="ui-nav__icon"', app)
        self.assertIn('class="ui-nav__brand"', app)
        self.assertIn('id="ui-nav-state-boot"', app)
        self.assertIn("document.documentElement.dataset.nav", app)
        self.assertNotIn("APP_NAV_CSS", app)
        self.assertNotIn("APP_NAV_BEHAVIOR", app)
        self.assertNotIn('re.sub(r"(<header', app)

    def test_navigation_selection_and_page_heading_contract(self) -> None:
        css = UI_CSS.read_text(encoding="utf-8")
        self.assertRegex(
            css,
            r"\.ui-nav__item\.active::before\s*\{[^}]*left:\s*-14px;[^}]*width:\s*3px;",
        )
        self.assertRegex(
            css,
            r"\.ui-page-heading__icon,\s*\.source-page-icon,\s*\.chat-page-icon\s*\{[^}]*display:\s*none;",
        )

    def test_chat_history_controls_are_shared_across_providers(self) -> None:
        chat = (STATIC_DIR / "chat.html").read_text(encoding="utf-8")
        app = (SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8")
        for contract in (
            'id="sessionSearchToggle"',
            'id="sessionSearchInput"',
            'id="sessionSearchClear"',
            'title="清除搜索" hidden',
            "function sessionGroup",
            "function sessionStamp",
            "function syncSessionSearchClear()",
            "m.role==='user'",
            "Math.min(innerHeight-height-8,picked.rect.top-height-8)",
            "fixedRootRect.left",
        ):
            self.assertIn(contract, chat)
        css = UI_CSS.read_text(encoding="utf-8")
        self.assertIn("::-webkit-search-cancel-button", css)
        self.assertIn(".session-search-clear[hidden]", css)
        self.assertIn("grid-template-columns: 44px minmax(0, 1fr) 44px", css)
        self.assertRegex(
            css,
            r"\.input-bar\s*\{[^}]*grid-template-columns:\s*44px minmax\(0, 1fr\) 44px;[^}]*align-items:\s*center;",
        )
        self.assertIn('id="expandInputBtn"', chat)
        self.assertIn('aria-expanded="false"', chat)
        self.assertIn('$("expandInputBtn").onclick', chat)
        self.assertIn(".composer.expanded textarea", css)
        self.assertIn(".attachment-preview:empty", css)
        self.assertNotIn('id="toolBtn"', chat)
        self.assertIn('$("headerToolBtn").onclick=openModal', chat)
        self.assertIn('id="headerToolBtn"', app)
        self.assertNotIn('class="chat-model-status"', app)
        self.assertIn('query.get("query", [""])[0]', app)
        self.assertIn("message.content", app)

    def test_inline_chat_scripts_are_valid_javascript(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for inline JavaScript syntax validation")
        chat = (STATIC_DIR / "chat.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", chat)
        for index, script in enumerate(scripts):
            if not script.strip():
                continue
            result = subprocess.run(
                [node, "--check", "-"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, f"inline script {index}: {result.stderr}")

    def test_ui_test_mode_blocks_mutations_before_handlers(self) -> None:
        app = (SCRIPTS_DIR / "web_app.py").read_text(encoding="utf-8")
        post = app.index("    def do_POST(self) -> None:", app.index("class Handler"))
        first_handler = app.index("handle_feishu_capability_post", post)
        guard = app.index("if UI_TEST_MODE:", post)
        self.assertLess(guard, first_handler)
        delete = app.index("    def do_DELETE(self) -> None:", post)
        delete_guard = app.index("if UI_TEST_MODE:", delete)
        self.assertLess(delete_guard, app.index('parsed.path.startswith("/amazon/")', delete))


if __name__ == "__main__":
    unittest.main()
