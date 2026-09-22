"""Offline editor obstruction, diagnostic precedence and multi-client dialog checks."""
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from browser_dialogs import observe_dialogs, own_page_dialogs
import tiktok_studio_publish as publish


def main():
    with TemporaryDirectory() as temp, sync_playwright() as pw:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox', f'--remote-debugging-port={port}'])
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        try:
            page.set_content('''<button onclick="window.wrong=true">Add</button>
                <button onclick="window.linked=(window.linked||0)+1">Add link</button>
                <div class="aside" style="position:fixed;inset:0;background:white">
                <div class="MusicPanelTabListMusicList__loading">Music</div>
                <button onclick="this.parentNode.remove()">Close</button></div>''')
            publish._open_product_link(page)
            assert page.evaluate('window.linked') == 1
            assert page.evaluate('window.wrong || false') is False
            page.set_content('''<button onclick="window.clicked=true">Add link</button>
                <div class="aside" style="position:fixed;inset:0"><div class="Timeline__movArea">Edit</div></div>''')
            try:
                publish._open_product_link(page)
            except publish.ManualReviewRequired as exc:
                assert '明确关闭按钮' in str(exc)
            else:
                raise AssertionError('Unknown editor was bypassed')
            assert page.evaluate('window.clicked || false') is False
            original = RuntimeError('description input timeout')
            with patch.object(publish, '_popup_snapshot', side_effect=RuntimeError('screenshot closed')):
                try:
                    publish._run_parameter_step(page, Path(temp), 'description', lambda: (_ for _ in ()).throw(original))
                except publish.ManualReviewRequired as exc:
                    assert exc.__cause__ is original
                    assert 'description input timeout' in str(exc)
                else:
                    raise AssertionError('Original error lost')
            own_page_dialogs(page)
            observer = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
            observe_dialogs(observer)
            for _ in range(3):
                assert page.evaluate("confirm('offline confirmation')") is False
                assert observer.contexts[0].pages[0].evaluate('2+3') == 5
            print('PASS: editor close, exact link selection, unknown-overlay handoff, original error and CDP observer dialogs')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
