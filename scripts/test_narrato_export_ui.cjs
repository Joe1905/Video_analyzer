// Run: node scripts/test_narrato_export_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/narrato_workbench_server.py`, 'utf8');
const code = source.slice(source.indexOf('    async function exportClips()'), source.indexOf('    function toggleClips('));
const elements = Object.fromEntries(['export-clips-btn', 'export-label', 'export-status'].map(id => [id, {
  disabled: false, textContent: '', classList: {add() {}, remove() {}},
  setAttribute() {}, removeAttribute() {}, appendChild(link) { this.link = link; }
}]));
let finish, calls = 0;
const context = vm.createContext({
  document: {getElementById: id => elements[id], createElement: () => ({}),
    querySelectorAll: () => [{getAttribute: () => '0'}]},
  appState: {currentTask: {id: 'sample'}}, window: {location: {}},
  fetch: () => { calls++; return new Promise(resolve => { finish = resolve; }); }
});
vm.runInContext(code, context);
(async () => {
  const pending = context.exportClips();
  assert.equal(elements['export-clips-btn'].disabled, true);
  assert.match(elements['export-status'].textContent, /正在剪辑/);
  await context.exportClips();
  assert.equal(calls, 1);
  finish({ok: true, json: async () => ({download_url: '/result.zip'})});
  await pending;
  assert.equal(elements['export-clips-btn'].disabled, false);
  assert.equal(context.window.location.href, '/result.zip');
  assert.equal(elements['export-status'].link.href, '/result.zip');
  const failed = context.exportClips();
  finish({ok: false, json: async () => ({error: '编码失败'})});
  await failed;
  assert.equal(elements['export-clips-btn'].disabled, false);
  assert.match(elements['export-status'].textContent, /编码失败/);
  console.log('Export UI checks passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
