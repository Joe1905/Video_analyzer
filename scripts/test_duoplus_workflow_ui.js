/* Offline UI state checks: a fake DOM and fetch, with no browser or network. */
const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const vm = require('vm');

async function main() {
  const html = fs.readFileSync(path.join(__dirname, 'static/duoplus.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements = {};
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    assert(!elements[match[1]], 'HTML IDs must be unique');
    elements[match[1]] = {value: '', files: [], disabled: false, hidden: false,
      textContent: '', checked: false, classList: {toggle() {}},
      setAttribute() {}, removeAttribute() {}, focus() {}};
  }
  Object.assign(elements.workflowBudget, {value: '300'});
  Object.assign(elements.workflowSteps, {value: '16'});
  Object.assign(elements.workflowTopics, {value: 'AI translator'});
  Object.assign(elements.workflowMode, {value: 'search'});
  elements.workflowPowerOff.checked = true;
  const template = JSON.parse(fs.readFileSync(path.join(__dirname, 'workflows/duoplus_tiktok_topic.json'), 'utf8'));
  const requests = [];
  let runs = 0, failNextPoll = false;
  const polls = {}, cancelled = new Set();
  const preview = {node_count: 37, search_available: true, model: 'offline-model'};
  const context = vm.createContext({
    document: {
      getElementById(id) { assert(elements[id], 'Missing HTML control: ' + id); return elements[id]; },
      querySelectorAll(selector) { return selector === '[data-action]' ? [] : Object.values(elements); }
    },
    setTimeout(callback) { callback(); },
    fetch: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({url, body});
      let data;
      if (url === '/api/duoplus/config') {
        data = {configured: false, image_id: 'offline-phone', region: 'cn'};
      } else if (url === '/api/duoplus/workflow') {
        data = {workflow: template, preview};
      } else if (url === '/api/duoplus/workflow/preview') {
        data = preview;
      } else if (url === '/api/duoplus/workflow/run') {
        data = {id: 'offline-job-' + ++runs, status: 'queued', budget_seconds: 300};
      } else if (url.startsWith('/api/duoplus/workflow/job?id=')) {
        if (failNextPoll) { failNextPoll = false; throw Error('Simulated polling failure'); }
        const id = url.split('id=')[1];
        const status = cancelled.has(id) ? 'cancelled' : (polls[id]++ ? 'complete' : 'running');
        if (!Number.isFinite(polls[id])) polls[id] = 1;
        data = {id, status, screen: {}, logs: [], budget_seconds: 300,
          auto_power_off: true, power_off_requested: true, error: cancelled.has(id) ? '用户已停止工作流。' : ''};
      } else if (url === '/api/duoplus/workflow/cancel') {
        cancelled.add(body.id);
        data = {id: body.id, message: '已请求停止。'};
      } else {
        throw Error('Unexpected API in offline UI test: ' + url);
      }
      return {ok: true, json: async () => data};
    }
  });
  await vm.runInContext(script, context);
  assert.equal(requests.length, 1); // Startup only uses the fake config.

  await elements.workflowSearch.onclick();
  const run = requests.find(item => item.url === '/api/duoplus/workflow/run');
  assert.equal(run.body.mode, 'search');
  assert.equal(run.body.topics, 'AI translator');
  assert.equal(run.body.budget_seconds, 300);
  assert.equal(run.body.auto_power_off, true);
  assert(elements.workflowCancel.disabled);
  assert(elements.status.textContent.includes('工作流完成'));
  console.log('PASS: topic, budget, completion and cleanup state');

  failNextPoll = true;
  await elements.workflowSearch.onclick();
  assert(!elements.workflowCancel.disabled, 'A lost status response must retain cancellation');
  await elements.workflowCancel.onclick();
  const cancel = requests.find(item => item.url === '/api/duoplus/workflow/cancel');
  assert.equal(cancel.body.id, 'offline-job-2');
  assert(elements.workflowCancel.disabled);
  assert(elements.status.textContent.includes('用户已停止'));
  console.log('PASS: cancellation remains available after polling failure');

  failNextPoll = true;
  await elements.workflowSearch.onclick();
  const count = runs;
  await elements.workflowSearch.onclick();
  assert.equal(runs, count, 'Recovering a poll must not enqueue another phone job');
  assert(elements.workflowCancel.disabled);
  console.log('PASS: polling recovery never duplicates a phone job');

  elements.workflowFile.files = [{size: 10, text: async () => '{invalid JSON'}];
  await elements.workflowFile.onchange();
  await elements.workflowSearch.onclick();
  assert.equal(runs, count, 'An invalid import must not silently execute the builtin template');
  assert(elements.status.textContent.includes('当前导入文件无效'));
  assert(!requests.some(item => item.url.includes('/phones') || item.url.endsWith('/screen') || item.url.endsWith('/run') && item.url !== '/api/duoplus/workflow/run'));
  console.log('PASS: invalid JSON cannot fall back to a different workflow');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
