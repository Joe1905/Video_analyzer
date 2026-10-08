"""Read-only UI checks; clipboard is stubbed and no analysis jobs are submitted."""
import os
from playwright.sync_api import sync_playwright


def main():
    base = os.getenv('COMMERCE_TEST_URL', 'http://192.168.1.254:4003')
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("window.testCopies=[];Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>window.testCopies.push(text)}})")
        page.goto(base + '/extract', wait_until='domcontentloaded')
        page.locator('#homeFiles .file-item').first.wait_for()
        page.locator('#reviewFilter').select_option('ready')
        page.locator('#reviewSearch').fill('7684226408121503007')
        row = page.locator('#homeFiles .file-item')
        row.wait_for(); assert row.count() == 1
        assert row.locator('.review-list-summary').inner_text()
        assert '历史快照' in row.inner_text()
        page.screenshot(path='/tmp/readable-review-list-desktop.png')
        row.get_by_role('button', name='查看复盘', exact=True).click()
        page.get_by_role('heading', name='表现诊断', exact=True).wait_for()
        assert not page.locator('.review-reference').evaluate('(el)=>el.open')
        assert '观察事实' in page.locator('.review-card').first.inner_text()
        assert '可能原因 · 待验证' in page.locator('.review-card').first.inner_text()
        page.get_by_role('button', name='复制复盘要点', exact=True).click()
        page.get_by_text('复盘要点已复制', exact=True).wait_for()
        copied = page.evaluate('window.testCopies[0]')
        assert '待验证' in copied and '39' in copied
        page.locator('.review-card .review-play').first.click()
        page.locator('#reviewPlayer').wait_for(state='visible')
        page.wait_for_function("document.getElementById('reviewVideo').readyState>=1")
        assert '7684226408121503007' in page.locator('#reviewVideo').get_attribute('src')
        page.locator('#reviewPlayerClose').click()
        assert page.locator('#reviewVideo').evaluate('(v)=>v.paused')
        page.locator('.review-reference > summary').click()
        page.get_by_role('heading', name='逐镜头执行表', exact=True).wait_for()
        page.locator('.review-reference > summary').click()
        page.locator('#out').evaluate('(el)=>el.scrollTop=0')
        page.screenshot(path='/tmp/readable-review-detail-desktop.png')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('#out').evaluate('(el)=>el.scrollTop=0')
        page.screenshot(path='/tmp/readable-review-detail-mobile.png')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        page.locator('#back').click()
        page.locator('#reviewSearch').fill('no-such-video-987654321')
        page.get_by_text('没有匹配的视频', exact=True).wait_for()
        assert not errors, errors
        print('PASS: list search/filter, review cards, clipboard, real video dialog, folded references, desktop/mobile, empty state; no JS errors')
        browser.close()


if __name__ == '__main__':
    main()
