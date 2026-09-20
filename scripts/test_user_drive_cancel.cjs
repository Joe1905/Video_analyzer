// File input cancel bubbles; only the dialog's own cancel should close it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, 'static/assets/user-drive.js'), 'utf8');
const handler = source.match(/dialog\.oncancel = event => \{[^\n]+\};/);
assert.ok(handler, 'Drive dialog cancel handler must exist');
let closed = 0;
const dialog = {};
vm.runInNewContext(handler[0], {dialog, close: () => closed++});
let prevented = false;
dialog.oncancel({target: {type: 'file'}, preventDefault() { prevented = true; }});
assert.equal(closed, 0, 'Cancelling the file chooser must keep the drive open');
assert.equal(prevented, false, 'Do not intercept the file input cancel event');
dialog.oncancel({target: dialog, preventDefault() { prevented = true; }});
assert.equal(closed, 1, 'Escape on the dialog must still use its close handler');
assert.equal(prevented, true, 'Prevent native close to preserve the upload-in-progress guard');
console.log('PASS: file chooser cancel stays open; dialog Escape retains close behavior');
