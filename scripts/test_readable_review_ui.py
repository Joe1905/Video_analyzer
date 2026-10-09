"""Read-only UI checks; clipboard is stubbed and no analysis jobs are submitted."""
import os
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    base = os.getenv('COMMERCE_TEST_URL', 'http://192.168.1.254:4003')
    with sync_playwright() as api:
        browser = api.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        console_errors = []
        console_warnings = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error'
                else console_warnings.append(message.text) if message.type == 'warning' else None)
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
        assert page.evaluate("reviewTime('1秒→2秒').start") == 1
        assert page.evaluate("reviewTime('0:01–0:03').end") == 3
        assert page.evaluate("reviewTime('采集快照2026-09-23')") is None
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
        page.wait_for_function("document.getElementById('reviewVideo').paused")
        assert page.locator('#reviewVideo').evaluate('(v)=>v.paused')
        page.locator('.review-reference > summary').click()
        page.get_by_role('heading', name='逐镜头执行表', exact=True).wait_for()
        page.locator('.review-reference > summary').click()
        page.locator('#out').evaluate("(el)=>{el.style.scrollBehavior='auto';el.scrollTop=0}")
        page.screenshot(path='/tmp/readable-review-detail-desktop.png')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('#out').evaluate('(el)=>el.scrollTop=0')
        page.screenshot(path='/tmp/readable-review-detail-mobile.png')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        assert page.locator('#out .hero').bounding_box()['y'] < 844
        page.locator('#reviewVideoSelect').select_option('shortvideo_SociaVault_7683856958457253150.mp4')
        page.wait_for_function("location.hash.includes('7683856958457253150') && document.getElementById('out').innerText.includes('389')")
        page.locator('#back').click()
        page.locator('#reviewSearch').fill('no-such-video-987654321')
        page.get_by_text('没有匹配的视频', exact=True).wait_for()
        # Verify the always-visible entry against the real library, before installing fixtures.
        library_page=browser.new_page(viewport={'width':1440,'height':1000})
        library_page.goto(base+'/metrics', wait_until='domcontentloaded')
        library_page.locator('#reviewLibraryLink').wait_for()
        library_page.wait_for_function("/\\d+/.test(document.getElementById('reviewLibraryCount').textContent)")
        library_page.screenshot(path='/tmp/review-entry-real-desktop.png')
        library_page.set_viewport_size({'width':390,'height':844})
        assert library_page.locator('#reviewLibraryLink').is_visible()
        library_page.screenshot(path='/tmp/review-entry-real-mobile.png')
        library_page.locator('#reviewLibraryLink').click()
        assert library_page.evaluate("reviewText('0–15.033333秒；11.466666666666667–13.0秒')")=='0–15秒；11.5–13秒'
        assert library_page.evaluate("reviewText('完播率3.234567%，2026-10-08，商品123456789')")=='完播率3.234567%，2026-10-08，商品123456789'
        assert library_page.evaluate("reviewText({start:4.8,end:15.033333})")=='开始：4.8秒\n结束：15秒'
        library_page.locator('#nativeReviewResult h3').filter(has_text='表现诊断|逻辑合理性').first.wait_for()
        assert '/metrics' in library_page.url
        assert library_page.locator('#nativeReviewSaved option').count()>=2
        library_page.locator('#nativeReviewSaved').select_option('shortvideo_SociaVault_7684226408121503007.mp4')
        library_page.wait_for_function("state.review.report['采集数据来源'].overview.play_count == 68")
        library_page.screenshot(path='/tmp/native-review-mobile.png')
        assert library_page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        library_page.set_viewport_size({'width':1440,'height':1000})
        library_page.screenshot(path='/tmp/native-review-desktop.png')
        library_page.close()
        # Library transport is stubbed using the real saved review samples; no collection jobs run.
        videos = []
        for vid in ('7683856958457253150', '7684226408121503007'):
            report = json.loads((Path('/workspace/output') / ('shortvideo_SociaVault_'+vid+'.mp4') / 'audit_result.json').read_text())
            context = report['采集数据来源']
            extracted = json.loads((Path('/workspace/output') / ('shortvideo_SociaVault_'+vid+'.mp4') / 'analysis.json').read_text())
            videos.append({'video_id':vid,'title':context['title'],'author':'neurobuddiestudio',
                           'views':int(context['overview']['play_count']), 'likes':int(context['engagement']['likes']),
                           'comments':int(context['engagement']['comments']), 'shares':int(context['engagement']['shares']),
                           'saves':int(context['engagement']['favorites']),
                           'duration':extracted['metadata'].get('duration_seconds') or extracted['metadata']['sampling_coverage']['duration_seconds'],
                           'url':'https://www.tiktok.com/@neurobuddiestudio/video/'+vid})
        page.route('**/api/product-videos/accounts?*', lambda route: route.fulfill(json={'products':[
            {'product_id':'account:24','product_name':'neurobuddiestudio','handle':'neurobuddiestudio'}]}))
        page.route('**/api/product-videos/list?*', lambda route: route.fulfill(json={'videos':videos,'job':None,'has_more':False}))
        page.set_viewport_size({'width':1440,'height':1000})
        page.goto(base+'/metrics', wait_until='domcontentloaded')
        page.locator('[data-review]').first.wait_for()
        page.locator('.video-review').first.wait_for()
        assert page.locator('.video-review').count() == 2
        assert page.get_by_role('button', name='播放 / 音频', exact=True).count() == 2
        assert '历史指标' in page.locator('.video-review').first.inner_text()
        page.screenshot(path='/tmp/readable-review-metrics-fixture.png')
        page.set_viewport_size({'width':390,'height':844})
        page.locator('.video-review').first.scroll_into_view_if_needed()
        page.screenshot(path='/tmp/readable-review-metrics-mobile-fixture.png')
        states={'value':'missing','starts':0}
        def review_response(route):
            vid=route.request.url.split('video_id=')[1].split('&')[0]
            report=json.loads((Path('/workspace/output')/('shortvideo_SociaVault_'+vid+'.mp4')/'audit_result.json').read_text())
            route.fulfill(json={'status':states['value'],'message':'测试处理中' if states['value']=='processing' else '复盘状态',
                'report':report if states['value']=='ready' else None,'video_url':'/video/shortvideo_SociaVault_'+vid+'.mp4'})
        def start_review(route):
            assert route.request.post_data_json['action']=='review'
            states.update(value='processing',starts=states['starts']+1)
            route.fulfill(json={'status':'running','action':'review'})
        page.route('**/api/product-videos/review?*',review_response)
        page.route('**/api/product-videos/jobs',start_review)
        page.locator('[data-review]').first.click()
        page.wait_for_function("document.getElementById('nativeReviewStatus').innerText.includes('测试处理中')")
        assert states['starts']==1
        states['value']='failed'
        page.locator('#nativeReviewRetry').wait_for(state='visible')
        page.locator('#nativeReviewRetry').click()
        page.wait_for_function("document.getElementById('nativeReviewStatus').innerText.includes('测试处理中')")
        assert states['starts']==2
        states['value']='ready'
        page.get_by_role('heading',name='表现诊断',exact=True).wait_for()
        assert '/metrics' in page.url
        page.locator('#copyNativeReview').click()
        assert '待验证' in page.evaluate('window.testCopies.at(-1)')
        page.locator('[data-close="reviewDialog"]').click()
        assert not page.locator('#reviewDialog').evaluate('(el)=>el.open')
        assert not errors, errors
        assert not console_errors, console_errors
        print('Console warnings:', len(console_warnings))
        print('PASS: list search/filter, review cards, clipboard, real video dialog, folded references, desktop/mobile, empty state, metrics links (saved-sample transport fixture); no JS errors')
        browser.close()


if __name__ == '__main__':
    main()
