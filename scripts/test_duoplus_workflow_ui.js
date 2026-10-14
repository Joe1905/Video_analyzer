/* Offline UI state checks: a fake DOM and fetch, with no browser or network. */
const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const vm = require('vm');

async function main() {
  const html = fs.readFileSync(path.join(__dirname, 'static/duoplus.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements = {};
  function element() {
    return {value: '', files: [], disabled: false, hidden: false, children: [],
      textContent: '', checked: false, attributes: {}, classList: {toggle() {}},
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; }, focus() {}, click() {},
      append(...children) { this.children.push(...children); }};
  }
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    assert(!elements[match[1]], 'HTML IDs must be unique');
    elements[match[1]] = element();
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
  const preview = {node_count: 37, ai_nodes: [], search_available: true, model: 'offline-model'};
  const context = vm.createContext({
    document: {
      createElement() { return element(); },
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
  assert.equal(elements.configView.hidden, false);
  assert(elements.configStatus.textContent.includes('请先配置'));

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
  elements.workflowCardFull.onclick();
  assert.equal(elements.workflowMode.value, 'search', 'A pending phone job must lock workflow selection');
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

  elements.workflowCardFull.onclick();
  assert.equal(elements.workflowMode.value, 'full');
  assert.equal(elements.workflowSelectedName.textContent, '主题养号');
  await elements.workflowSearch.onclick();
  assert.equal(requests.filter(item => item.url === '/api/duoplus/workflow/run').at(-1).body.mode, 'full');
  console.log('PASS: builtin cards choose the correct execution mode');

  const importedA = {...template, fixture: 'A'}, importedB = {...template, fixture: 'B'};
  elements.workflowFile.files = [
    {name: '流程 A.json', size: 100, text: async () => JSON.stringify(importedA)},
    {name: '流程 B.json', size: 100, text: async () => JSON.stringify(importedB)}
  ];
  const beforeImport = runs;
  await elements.workflowFile.onchange();
  assert.equal(runs, beforeImport, 'Import and selection must never operate the phone');
  assert.equal(elements.workflowCards.children.length, 2);
  assert.equal(elements.workflowSelectedName.textContent, '流程 A');
  const secondCard = elements.workflowCards.children[1];
  secondCard.onclick();
  assert.equal(elements.workflowSelectedName.textContent, '流程 B');
  assert.equal(secondCard.attributes['aria-pressed'], 'true');
  await elements.workflowSearch.onclick();
  const importedRun = requests.filter(item => item.url === '/api/duoplus/workflow/run').at(-1);
  assert.equal(importedRun.body.workflow.fixture, 'B', 'Run must use the selected card payload');
  assert.equal(importedRun.body.mode, 'full');
  elements.workflowCardSearch.onclick();
  await elements.workflowSearch.onclick();
  const builtinRun = requests.filter(item => item.url === '/api/duoplus/workflow/run').at(-1);
  assert.equal(builtinRun.body.workflow.fixture, undefined, 'Imported payload must not leak into builtin cards');
  assert.equal(builtinRun.body.mode, 'search');
  console.log('PASS: multiple imports route only the selected workflow and cannot leak into builtins');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
