"""Offline Chromium regressions for the observed October 2026 and legacy pickers."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import sync_playwright
import tiktok_studio_publish as publish


RADIOS = '''<label><input type="radio" name="postSchedule" value="post_now">Now</label>
<label><input type="radio" name="postSchedule" value="schedule" checked>Schedule</label>'''

NEW_PICKERS = '''<div data-e2e="schedule_container">
<div role="combobox" aria-label="Select date" aria-expanded="false" id="date"
 onclick="calendar.hidden=!calendar.hidden;this.setAttribute('aria-expanded',String(!calendar.hidden))">
 <span class="Picker__value">Jul 16, 2026</span></div>
<div role="combobox" aria-label="Select time" aria-expanded="false" id="time"
 onclick="clock.hidden=!clock.hidden;this.setAttribute('aria-expanded',String(!clock.hidden))">
 <span class="Picker__value">08:00</span></div></div>
<div class="DatePicker__panel" id="calendar" hidden>
 <button aria-label="Previous month" onclick="move(-1)">Previous</button>
 <button class="DatePicker__titleMonth"></button><button class="DatePicker__titleYear"></button>
 <button aria-label="Next month" onclick="move(1)">Next</button><div id="days"></div></div>
<div id="clock" hidden><div role="listbox" aria-label="Hours"></div>
 <div role="listbox" aria-label="Minutes"></div></div>
<button onclick="window.posts++">Schedule</button>
<script>
window.posts=0; window.month=new Date(2026,6,1); window.hh='08'; window.mm='00';
function move(step){month.setMonth(month.getMonth()+step);render();}
function render(){
 document.querySelector('.DatePicker__titleMonth').textContent=month.toLocaleString('en-US',{month:'long'});
 document.querySelector('.DatePicker__titleYear').textContent=month.getFullYear();
 days.innerHTML=`<button class="DatePicker__day" data-outside="true" onclick="throw Error('outside month')">25</button>`;
 for(let i=1;i<=31;i++){
  let b=document.createElement('button'); b.className='DatePicker__day'; b.dataset.outside='false';
  b.dataset.disabled=String(i===30); b.disabled=i===30; b.textContent=i;
  b.onclick=()=>{let value=month.toLocaleString('en-US',{month:'short'})+' '+i+', '+month.getFullYear();
   setTimeout(()=>document.querySelector('#date .Picker__value').textContent=value,250);
   calendar.hidden=true;date.setAttribute('aria-expanded','false');};days.append(b);
 }
}
for(let label of ['Hours','Minutes']){
 let column=document.querySelector('[aria-label="'+label+'"]');
 for(let i=0;i<(label==='Hours'?24:60);i+=(label==='Hours'?1:5)){
  let value=String(i).padStart(2,'0'), b=document.createElement('button');
  b.setAttribute('role','option');b.setAttribute('aria-label',label+' '+value);b.textContent=value;
  b.onclick=()=>{if(label==='Hours')hh=value;else mm=value;
   let selected=hh+':'+mm;setTimeout(()=>{if(!window.rejectTime)document.querySelector('#time .Picker__value').textContent=selected;},250);};
  column.append(b);
 }
}
render();
</script>'''


def main():
    with sync_playwright() as pw, TemporaryDirectory() as tmp:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        page.goto("https://www.tiktok.com/tiktokstudio/upload")
        try:
            with patch.object(publish, "TIMEZONE_NAME", "America/Los_Angeles"):
                page.set_content(RADIOS + NEW_PICKERS)
                publish._set_schedule(page, "tiktok", "2026-10-25T17:23:00Z", Path(tmp))
                assert page.locator('#date .Picker__value').inner_text() == "Oct 25, 2026"
                assert page.locator('#time .Picker__value').inner_text() == "10:25"
                assert page.locator('#time').get_attribute('aria-expanded') == 'false'
                assert page.evaluate('window.posts') == 0
                # A repeated parameter step must not reopen or submit the form.
                publish._set_schedule(page, "tiktok", "2026-10-25T17:23:00Z", Path(tmp))
                publish._select_custom_date(page, page.locator('#date'), datetime(2027, 1, 25))
                assert page.locator('#date .Picker__value').inner_text() == "Jan 25, 2027"
                publish._select_custom_date(page, page.locator('#date'), datetime(2026, 12, 25))
                assert page.locator('#date .Picker__value').inner_text() == "Dec 25, 2026"
                print('PASS: new pickers, rounding/timezone, month/year navigation, repeated step, no submission')

                try:
                    publish._select_custom_date(page, page.locator('#date'), datetime(2026, 12, 30))
                except RuntimeError as exc:
                    assert '没有可选日期' in str(exc)
                else:
                    raise AssertionError('Disabled date accepted')
                page.set_content(RADIOS + NEW_PICKERS)
                page.evaluate('window.rejectTime=true')
                try:
                    publish._select_custom_time(page, page.locator('#time'), datetime(2026, 10, 25, 10, 25))
                except RuntimeError as exc:
                    assert '期望 10:25，当前 08:00' in str(exc)
                else:
                    raise AssertionError('Unchanged time accepted')
                print('PASS: disabled date and uncommitted time rejected')

                page.set_content(RADIOS + '''<div data-e2e="schedule_container">
                 <input id="time" class="TUXTextInputCore-input" readonly value="08:00"
                  onclick="document.querySelector('.tiktok-timepicker-time-picker-container').hidden=false">
                 <input id="date" class="TUXTextInputCore-input" readonly value="2026-10-24"
                  onclick="document.querySelector('.calendar-wrapper').hidden=false"></div>
                 <div class="calendar-wrapper" hidden><span class="month-title">October</span>
                  <span class="year-title">2026</span><span class="day valid"
                  onclick="document.querySelector('#date').value='2026-10-25';this.parentElement.hidden=true">25</span></div>
                 <div class="tiktok-timepicker-time-picker-container" style="height:100px" hidden>
                  <div class="tiktok-timepicker-option-list"><span class="tiktok-timepicker-option-text">10</span></div>
                  <div class="tiktok-timepicker-option-list"><span class="tiktok-timepicker-option-text"
                   onclick="setTimeout(()=>document.querySelector('#time').value='10:25',250)">25</span></div></div>''')
                # Old UI opens the time panel from the input's wrapper.
                page.locator('[data-e2e=schedule_container]').evaluate("el=>el.onclick=()=>document.querySelector('.tiktok-timepicker-time-picker-container').hidden=false")
                publish._set_schedule(page, "tiktok", "2026-10-25T17:25:00Z", Path(tmp))
                assert page.locator('#date').input_value() == '2026-10-25'
                assert page.locator('#time').input_value() == '10:25'
                print('PASS: legacy readonly calendar and time columns')

                page.set_content(RADIOS + '<input type="date"><input type="time">')
                publish._set_schedule(page, "tiktok", "2026-10-25T17:25:00Z", Path(tmp))
                assert page.locator('input[type=date]').input_value() == '2026-10-25'
                assert page.locator('input[type=time]').input_value() == '10:25'
                with patch.object(publish, '_handle_parameter_popup', return_value=False):
                    publish._set_schedule(page, "server", "", Path(tmp))
                assert page.locator('input[value=post_now]').is_checked()
                print('PASS: native date/time inputs and immediate-post selection')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
