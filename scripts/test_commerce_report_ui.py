"""Read-only browser smoke check after generating the two review fixtures on 4003."""
import os
from playwright.sync_api import sync_playwright


def main():
    base = os.getenv("COMMERCE_TEST_URL", "http://127.0.0.1:4003")
    filename = "shortvideo_SociaVault_7683856958457253150.mp4"
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/extract#detail=" + filename, wait_until="domcontentloaded")
        page.locator("#out .hero h2").wait_for()
        assert "提取内容报告" in page.locator("#out").inner_text()
        page.locator('[data-tab="audit"]').click()
        page.get_by_role("heading", name="表现诊断", exact=True).wait_for()
        text = page.locator("#out").inner_text()
        for expected in ("内容拆解", "优先修改", "表现快照", "观察事实", "可能原因 · 待验证", "389", "7.7%"):
            assert expected in text, expected
        page.screenshot(path="/tmp/commerce-review-desktop.png", full_page=True)
        page.locator("#detailPrompt").click()
        assert "仅用于报告" in page.locator("#promptModal").inner_text()
        page.locator("#promptClose").click()
        # Intercept only this action to verify request separation without buying another report.
        requests = []
        def capture(route):
            requests.append(route.request.post_data_json)
            route.fulfill(status=202, content_type="application/json", body='{"status":"queued"}')
        page.route("**/api/postprocess", capture)
        page.locator("#detailReport").click()
        page.wait_for_timeout(500)
        assert requests and "report_prompt" in requests[-1]
        assert "analysis_prompt" not in requests[-1]
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/commerce-review-mobile.png", full_page=True)
        assert not errors, errors
        for video_id in ("7683856958457253150", "7684226408121503007"):
            page.goto(base + "/extract#detail=shortvideo_SociaVault_" + video_id + ".mp4", wait_until="domcontentloaded")
            page.locator("#out .hero h2").wait_for()
            page.locator('[data-tab="audit"]').click()
            page.locator('.review-reference > summary').click()
            page.get_by_role("heading", name="原片改剪表", exact=True).wait_for()
            page.get_by_role("heading", name="逐镜头执行表", exact=True).wait_for()
            page.get_by_role("heading", name="原片镜头拆解", exact=True).wait_for()
            assert "台词：" in page.locator("#out").inner_text()
            assert "原片素材：" in page.locator("#out").inner_text()
        assert not errors, errors
        print("PASS: content/report tabs, collection evidence, report-only prompt, desktop/mobile, no JS errors")
        browser.close()


if __name__ == "__main__":
    main()
