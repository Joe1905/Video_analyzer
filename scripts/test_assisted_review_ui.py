"""Actual saved evidence reports, seek/copy/mobile, correction transport without paid jobs."""
import copy
import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.add_init_script("window.testCopies=[];Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>window.testCopies.push(text)}})")
        page.goto('http://127.0.0.1:4003/metrics',wait_until='domcontentloaded')
        page.locator('[data-review="7693858842249055501"]').click()
        page.get_by_role('heading',name='1. 视频逻辑与统一时间轴',exact=True).wait_for()
        assert page.locator('.review-timeline tbody tr').count()==22
        assert '未提供有效逐秒留存' in page.locator('#nativeReviewResult').inner_text()
        assert '0.8秒' in page.locator('#review-row-t0').inner_text()
        page.locator('#review-row-t4 [data-review-seek]').click()
        page.wait_for_function("document.querySelector('#nativeReviewPreview video').currentTime >=4")
        page.locator('#nativeReviewPreview video').evaluate('(v)=>v.pause()')
        assert page.locator('#nativeReviewPreview video').evaluate('(v)=>v.currentTime')<5.5
        page.locator('#copyNativeReview').click()
        assert '视频逻辑' in page.evaluate('window.testCopies[0]')
        page.screenshot(path='/tmp/evidence-review-desktop.png')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path='/tmp/evidence-review-mobile.png')
        page.set_viewport_size({'width':1440,'height':1000})
        # Verify actual correction payload; prevent a paid reanalysis in browser QA.
        report=json.loads(Path('/workspace/output/shortvideo_SociaVault_7693858842249055501.mp4/assisted_review.json').read_text())
        submitted=[]
        def job(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(json={'action':'review','video_id':'7693858842249055501','status':'running'})
        def review(route):
            updated=copy.deepcopy(report);updated['人工补充']=submitted[0]['logic_note']
            route.fulfill(json={'status':'ready','report':updated,'message':'复盘已完成。',
                'video_url':'/video/shortvideo_SociaVault_7693858842249055501.mp4'})
        page.route('**/api/product-videos/jobs',job)
        page.route('**/api/product-videos/review?*',review)
        page.locator('summary').filter(has_text='补充或纠正视频逻辑').click()
        note='主推Vivi玩偶，胸针是剧情道具。'
        page.locator('#nativeLogicNote').fill(note)
        page.locator('#regenerateNativeReview').click()
        page.wait_for_function("state.review.report?.['人工补充']==='主推Vivi玩偶，胸针是剧情道具。'")
        assert submitted[0]['force'] is True and submitted[0]['logic_note']==note
        page.locator('[data-close="reviewDialog"]').click()
        page.locator('#reviewLibraryLink').click()
        page.locator('#nativeReviewSaved').select_option('shortvideo_SociaVault_7684226408121503007.mp4')
        page.wait_for_function("state.review.report?.['采集数据来源']?.video_id==='7684226408121503007'")
        assert '39 个百分点' in page.locator('#nativeReviewResult').inner_text()
        assert page.get_by_text('变化前',exact=True).count()>0
        assert page.get_by_text('变化中',exact=True).count()>0
        assert page.get_by_text('变化后',exact=True).count()>0
        page.screenshot(path='/tmp/evidence-retention-desktop.png')
        assert not errors,errors
        print('PASS actual timeline, missing retention, 39pp window context, video seek, copy, mobile, correction transport; no JS errors.')
        browser.close()


if __name__=='__main__':main()
