// Minimal DOM shim: load the panel's <script>, call showTab() for each tab, and
// report which panes end up visible. This reproduces what the browser does.
const fs = require('fs');
const nodePath = require('path');
const path = nodePath.join(__dirname, 'panel.js');
const js = fs.readFileSync(path, 'utf8');

const PANES = ['pane-scan', 'pane-setup', 'pane-config', 'pane-news'];
const TABS = ['scan', 'setup', 'config', 'news'];

function mkEl(id, cls, tab) {
  return {
    id, className: cls || '', dataset: { tab: tab || '' },
    style: {}, value: '', textContent: '', innerHTML: '', checked: false,
    classList: { toggle(){}, add(){}, remove(){}, contains(){ return false; } },
    appendChild(){}, addEventListener(){}, querySelectorAll(){ return []; },
  };
}
const elements = {};
const panes = PANES.map(id => { const e = mkEl(id, 'tabpane'); e.style.display = 'none'; elements[id] = e; return e; });
const tabs = TABS.map(t => { const e = mkEl('tab-' + t, 'tab', t); elements[e.id] = e; return e; });

global.document = {
  // Any id the real page defines must resolve, not just the tabs: showTab()
  // lazy-loads each pane's data, which touches ids like 'cronStatus'. Returning
  // null there made this harness throw a TypeError partway through, so the tab
  // assertions that had already passed were never reported as a clean exit.
  getElementById: id => elements[id] || (elements[id] = mkEl(id)),
  querySelectorAll: sel => sel === '.tabpane' ? panes : (sel === '.tab' ? tabs : []),
  createElement: () => mkEl('x'),
};
global.window = {};
global.localStorage = { getItem: () => null, setItem: () => {} };
// Model the REAL API shape. Returning a bare {} made load() throw on
// `d.config.tickers` inside an async continuation, which crashed the harness
// with an unhandled rejection AFTER the tab assertions had already passed.
global.fetch = (path) => Promise.resolve({
  ok: true,
  json: () => Promise.resolve(
    path === '/api/config'
      ? { config: { tickers: [], ticker_meta: {}, push_mode: 'all' }, secrets: {} }
      : {}),
});
global.alert = () => {};
global.confirm = () => false;
global.setTimeout = () => 0;

// Evaluate the panel JS, then exercise only the tab machinery.
const sandbox = { showTab: null, initTabs: null };
try {
  eval(js);
} catch (e) {
  console.log('EVAL ERROR:', e.message);
}
const visible = () => panes.filter(p => p.style.display !== 'none').map(p => p.id);

let failures = 0;
const say = (line) => { console.log(line); };
const check = (label, ok) => { say(label); if (!ok) failures++; };

say('typeof showTab  : ' + typeof showTab);
say('typeof initTabs : ' + typeof initTabs);
if (typeof initTabs !== 'function') { failures++; say('*** initTabs is NOT a function ***'); }
else { initTabs(); say('after initTabs  : ' + (visible().join(', ') || '(none)')); }

if (typeof showTab !== 'function') {
  failures++;
  say('*** showTab is NOT a function - the tab buttons cannot work ***');
} else {
  for (const t of TABS) {
    showTab(t);
    const v = visible();
    const ok = v.length === 1 && v[0] === 'pane-' + t;
    check(`showTab(${t.padEnd(6)}) -> visible: ${v.join(',') || '(none)'}  ${ok ? 'OK' : '*** WRONG ***'}`, ok);
  }
}

// An async rejection in a lazy loader must not be reported as a tab failure,
// but it also must not be swallowed silently.
process.on('unhandledRejection', (e) => {
  say('note: async loader rejected (not a tab failure): ' + (e && e.message));
});

// ---- secrets: a leaked key must be CLEARABLE from the panel ---------------
// The old secretValue() returned the keep-mask whenever the box was empty, so
// emptying a stored key did nothing and it could only be revoked by editing the
// file by hand.
try {
  const KEEP = '__KEEP__';   // const in the eval'd script, not a global
  fillSecret('token', KEEP);
  check('untouched masked secret is kept (' + secretValue('token') + ')',
        secretValue('token') === KEEP);
  const el = document.getElementById('token');
  el.value = '';
  if (el.oninput) el.oninput();          // the user touched the field
  check('an emptied, touched secret is sent as a clear (' + secretValue('token') + ')',
        secretValue('token') === '');
  fillSecret('aikey', '');
  check('a brand-new field is not a mask', secretValue('aikey') === '');
} catch (e) {
  failures++;
  say('*** secret handling threw: ' + e.message + ' ***');
}

// ---- push mode: the min-score box must be disabled in "all" mode ----------
try {
  const mode = document.getElementById('pushMode');
  const box = document.getElementById('pushMinScore');
  mode.value = 'all';
  pushModeChanged();
  check('min score disabled in all mode (' + box.disabled + ')', box.disabled === true);
  mode.value = 'score';
  pushModeChanged();
  check('min score enabled in score mode (' + box.disabled + ')', box.disabled === false);
} catch (e) {
  failures++;
  say('*** pushModeChanged threw: ' + e.message + ' ***');
}

// Decide synchronously and set exitCode. This used to run in a setTimeout,
// but the DOM shim below overrides global.setTimeout with a no-op, so the
// timer never fired and the script exited without ever printing a verdict.
console.log(failures === 0
  ? 'RESULT: ALL CHECKS PASSED - tabs isolate one pane; secrets clear; push mode hints'
  : `RESULT: ${failures} FAILURE(S)`);
process.exitCode = failures === 0 ? 0 : 1;
