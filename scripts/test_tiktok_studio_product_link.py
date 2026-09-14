#!/usr/bin/env python3
"""Offline browser regression for ID selection and renamed product labels."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import tiktok_studio_publish as publish


def main() -> None:
    product_id = "1732591319412675493"
    label = "Rechargeable RC Bulldozer & Ma"
    with sync_playwright() as playwright, TemporaryDirectory() as directory:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        try:
            page.set_content(f"""
                <p>{label}</p>
                <button onclick="document.querySelector('[role=dialog]').hidden=false">Add link</button>
                <div role="dialog" hidden>
                  <h2>Add product links</h2>
                  <table><tr><td>{product_id}0</td><td>Wrong product</td></tr>
                  <tr><td>{product_id}</td><td><input type="radio"></td></tr></table>
                  <button onclick="document.querySelector('#detail').hidden=false">Next</button>
                  <div id="detail" hidden>
                    <p>Product name will appear on your video</p>
                    <input value="{label}">
                    <button onclick="document.querySelector('[role=dialog]').hidden=true;
                      const tag=document.createElement('span');
                      tag.textContent=document.querySelector('#detail input').value;
                      document.body.appendChild(tag)">Add</button>
                  </div>
                </div>
            """)
            product = {"product_id": product_id, "product_name": "pop工程车"}
            with patch.object(publish, "_selected_product", return_value=product):
                assert publish._add_product_link(page, product_id, Path(directory)) is False
            assert page.locator("input[type=radio]").is_checked()
            assert page.get_by_text(label, exact=True).count() == 2

            # An existing description, hidden label, or shared title prefix is not a new binding.
            page.set_content(f"<p>{label}</p><span hidden>{label}</span><p>{label} OTHER</p>")
            assert publish._wait_for_linked_product(page, label, 10, previous_count=1) is None
            page.set_content(f"<table><tr><td>{product_id}0</td></tr></table>")
            assert publish._find_product_row(page, product_id) is None
            print("PASS: exact product ID selection and renamed-label binding regression")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
