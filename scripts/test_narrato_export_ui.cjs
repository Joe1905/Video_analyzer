// Run: node scripts/test_narrato_export_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/narrato_workbench_server.py`, 'utf8');
const code = source.slice(source.indexOf('    let exportPoll;'), source.indexOf('    function toggleClips('));
const elements = Object.fromEntries(['export-clips-btn', 'export-label', 'export-status'].map(id => [id, {
  disabled: false, dataset: {}, textContent: '', classList: {add() {}, remove() {}},
  setAttribute() {}, removeAttribute() {}, appendChild(link) { this.link = link; }
}]));
let finish, calls = 0, selected = '0';
const context = vm.createContext({
  document: {getElementById: id => elements[id], createElement: () => ({}),
    querySelectorAll: () => [{getAttribute: () => selected}]},
  appState: {currentTask: {id: 'sample'}}, window: {location: {}},
  setTimeout: () => 1, clearTimeout() {},
  fetch: () => { calls++; return new Promise(resolve => { finish = resolve; }); }
});
vm.runInContext(code, context);
(async () => {
  const pending = context.exportClips();
  assert.equal(elements['export-clips-btn'].disabled, true);
  assert.match(elements['export-label'].textContent, /正在提交/);
  await context.exportClips();
  assert.equal(calls, 1);
  finish({ok: true, json: async () => ({status:'running', message:'正在剪辑 1/2'})});
  await pending;
  assert.equal(elements['export-clips-btn'].disabled, true);
  const polling = context.refreshExportStatus();
  finish({ok:true, json:async () => ({status:'complete', selected:[0], download_url:'/result.zip'})});
  await polling;
  assert.equal(elements['export-clips-btn'].disabled, false);
  assert.equal(elements['export-label'].textContent, '下载 ZIP');
  await context.exportClips();
  assert.equal(context.window.location.href, '/result.zip');
  assert.equal(calls, 2); // Download must not submit another export.
  selected = '1';
  vm.runInContext('showExportState(lastExportState)', context);
  assert.equal(elements['export-label'].textContent, '重新导出 ZIP');
  assert.equal(elements['export-clips-btn'].dataset.downloadUrl, '');
  const failed = context.exportClips();
  finish({ok: false, json: async () => ({error: '编码失败'})});
  await failed;
  assert.equal(elements['export-clips-btn'].disabled, false);
  assert.match(elements['export-status'].textContent, /编码失败/);
  console.log('Export UI checks passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
