// Gate: an unreadable config must disable Upload (the data-loss chain).
//
// The panel reads config_local.json with a BOM-tolerant loader. When the file
// exists but cannot be parsed, /api/config reports `read_errors`; the page must
// then disable "Upload config to server" and say why, because every field is
// blank and one click would write that emptiness over the real watchlist and
// API keys. The panel's secret fields are also exercised here: emptying a
// stored key must send a clear, not the keep-mask.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.dirname(__dirname);
const js = fs.readFileSync(path.join(__dirname, 'panel.js'), 'utf8');

let failures = 0;
function check(label, ok, detail) {
  console.log(`${label}${detail ? '  (' + detail + ')' : ''}`);
  if (!ok) failures++;
}

function makeEl(id) {
  return {
    id, style: {}, dataset: {}, value: '', textContent: '', innerHTML: '',
    checked: false, disabled: false, title: '', placeholder: '',
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    appendChild() {}, addEventListener() {}, querySelectorAll() { return []; },
    oninput: null,
  };
}

function boot(apiConfig) {
  const els = {};
  const document = {
    getElementById: (id) => els[id] || (els[id] = makeEl(id)),
    querySelectorAll: () => [],
    createElement: () => makeEl('x'),
  };
  const sandbox = {
    document,
    window: {},
    localStorage: { getItem: () => null, setItem: () => {} },
    // Every endpoint the lazy tab loaders touch needs a plausible shape: a
    // shared `{}` made refreshStatus throw on d.auth.authed (an unhandled
    // rejection that would fail this harness for the wrong reason).
    fetch: (p) => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(
        p === '/api/config' ? apiConfig :
        p === '/api/status' ? { gcloud: true, auth: { authed: false }, vm: null } :
        p === '/api/cron' ? { ok: true, cron: {} } :
        p === '/api/news' ? { ok: true, news: [] } :
        p === '/api/lookup' ? { ok: true, lookup: {} } :
        p === '/api/tavily' ? { tavily: {}, exa: {} } : {}),
    }),
    setTimeout: () => 0,
    confirm: () => false,
    alert: () => {},
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox);
  return new Promise((resolve) => setImmediate(() => resolve({ sandbox, document, els })));
}

(async () => {
  const healthy = await boot({
    config: { tickers: ['AAPL'], ticker_meta: {}, push_mode: 'all' },
    secrets: {}, defaults: {},
  });
  check('healthy config does not set _configBroken',
        !healthy.sandbox.window._configBroken);
  check('healthy config leaves Upload enabled',
        healthy.document.getElementById('uploadBtn').disabled !== true);

  const broken = await boot({
    config: { tickers: [], ticker_meta: {}, push_mode: 'all' },
    secrets: {}, defaults: {},
    read_errors: { 'config_local.json': 'Expecting value: line 1 column 14' },
  });
  const msg = String(broken.document.getElementById('msg').textContent || '');
  check('unreadable config sets _configBroken', broken.sandbox.window._configBroken === true);
  check('unreadable config disables Upload',
        broken.document.getElementById('uploadBtn').disabled === true);
  check('the page explains why', /could not be read/i.test(msg) && /disabled/i.test(msg),
        msg.slice(0, 90));
  check('the allowlist field exists', !!broken.document.getElementById('webEmails'));

  // Secret clearing: fillSecret + a touch means "clear", untouched means keep.
  const panel = healthy;
  const fn = panel.sandbox;
  fn.fillSecret('token', '__KEEP__');
  check('untouched secret keeps the stored value',
        fn.secretValue('token') === '__KEEP__');
  const token = panel.document.getElementById('token');
  token.value = '';
  if (token.oninput) token.oninput();
  check('an emptied, touched secret is a clear', fn.secretValue('token') === '');

  console.log(failures === 0
    ? 'RESULT: ALL CHECKS PASSED - unreadable config cannot be uploaded; secrets clear'
    : `RESULT: ${failures} FAILURE(S)`);
  process.exitCode = failures === 0 ? 0 : 1;
})();
