"""Offline regressions for observed product labels and asynchronous schedule UI."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import tiktok_studio_publish as publish


def main():
    with sync_playwright() as pw, TemporaryDirectory() as tmp:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        page.goto("https://www.tiktok.com/tiktokstudio/upload")
        try:
            page.set_content('''<section><div>Add link</div><button>+ Add</button>
                <span>Light Sword 360Spin RGB</span></section>''')
            assert publish._product_link_field_button(page) is not None
            assert publish._linked_product_labels(page, "Light Sword – 360° Spin + RGB")
            assert not publish._linked_product_labels(page, "Light Sword – 360° Spin + RGB OTHER")
            product = {"product_id": "12345", "product_name": "Light Sword – 360° Spin + RGB"}
            with patch.object(publish, "_selected_product", return_value=product):
                try:
                    publish._add_product_link(page, "12345", Path(tmp))
                except publish.ProductLinkReviewRequired:
                    pass
                else:
                    raise AssertionError("Existing unknown binding must not be added again")
                page.locator("span").evaluate("el => el.dataset.productId='12345'")
                assert publish._add_product_link(page, "12345", Path(tmp)) is False
            page.set_content('''<section><div>Add link</div><button>+ Add</button></section>
                <div contenteditable="true">Light Sword 360Spin RGB</div>''')
            assert not publish._linked_product_labels(page, "Light Sword – 360° Spin + RGB")
            page.set_content('<p>Video published</p><button onclick="window.clicked=true">Add link</button>')
            try:
                publish._open_product_link(page)
            except publish.ResultUncertain:
                pass
            else:
                raise AssertionError("Success conflict must block another submission")
            assert not page.evaluate("window.clicked || false")
            print("PASS: normalized field-only label, existing ID guard, success conflict")

            page.set_content('''<section id="links"><div>Add link</div>
                <button onclick="document.querySelector('[role=dialog]').hidden=false">+ Add</button></section>
                <div role="dialog" hidden><h2>Add product links</h2>
                  <table><tr><td>12345</td><td><input type="radio"></td></tr></table>
                  <button onclick="this.disabled=true;setTimeout(()=>document.querySelector('#detail').hidden=false,16000)">Next</button>
                  <div id="detail" hidden><p>Product name will appear on your video</p>
                    <input value="Light Sword – 360° Spin + RGB">
                    <button onclick="document.querySelector('[role=dialog]').hidden=true;
                      document.querySelector('#links').insertAdjacentHTML('beforeend','<span>Light Sword 360Spin RGB</span>')">Add</button>
                  </div>
                </div>''')
            with patch.object(publish, "_selected_product", return_value=product), patch.object(
                publish, "_handle_parameter_popup", side_effect=AssertionError("Loading is not an unknown popup")
            ):
                assert publish._add_product_link(page, "12345", Path(tmp)) is False
            print("PASS: Next loading beyond 15 seconds and normalized binding complete without popup recovery")

            page.set_content('''<div><input id="time" value="08:00"></div>
                <div class="tiktok-timepicker-time-picker-container" style="height:100px">
                  <div class="tiktok-timepicker-option-list"><span class="tiktok-timepicker-option-text">05</span></div>
                  <div class="tiktok-timepicker-option-list"><span class="tiktok-timepicker-option-text"
                    onclick="setTimeout(()=>document.querySelector('#time').value='05:05',900)">05</span></div>
                </div>''')
            publish._select_custom_time(page, page.locator("#time"), datetime(2026, 9, 25, 5, 5))
            assert page.locator("#time").input_value() == "05:05"
            # Wrong values must still fail; reporting uses the same observed value.
            page.locator("#time").fill("08:00")
            page.locator("[onclick]").evaluate("el => el.removeAttribute('onclick')")
            try:
                publish._select_custom_time(page, page.locator("#time"), datetime(2026, 9, 25, 5, 5))
            except RuntimeError as exc:
                assert "期望 05:05，当前 08:00" in str(exc)
            else:
                raise AssertionError("Incorrect time accepted")
            print("PASS: delayed time update accepted; wrong time rejected")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
