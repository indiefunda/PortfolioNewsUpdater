// Ask the real page, after a REAL wait, whether the sign-in gate appeared.
// Chrome's --virtual-time-budget distorts timer/IndexedDB behaviour, which is
// exactly what Firebase Auth depends on, so this drives a genuine clock.
const http = require('http');

const PORT = 9333;
const URL_TO_TEST = process.argv[2] || 'https://keen-wavelet-275120.web.app';
const WAIT_MS = parseInt(process.argv[3] || '15000', 10);

function targets() {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${PORT}/json`, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => resolve(JSON.parse(d)));
    }).on('error', reject);
  });
}

(async () => {
  const list = await targets();
  const page = list.find((t) => t.type === 'page');
  if (!page) {
    console.log('no page target found');
    process.exit(1);
  }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  };
  const send = (method, params) => new Promise((res) => {
    const i = ++id; pending.set(i, res);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
  await new Promise((r) => (ws.onopen = r));

  await send('Page.enable');
  await send('Page.navigate', { url: URL_TO_TEST });
  console.log(`navigated, waiting ${WAIT_MS}ms of REAL time...`);
  await new Promise((r) => setTimeout(r, WAIT_MS));

  const res = await send('Runtime.evaluate', {
    returnByValue: true,
    expression: `JSON.stringify({
      config: typeof window.__GL_FIREBASE_CONFIG__,
      hosted: window.__GL_HOSTED__ === true,
      signinGate: !!document.getElementById('signin'),
      signInButton: !!document.getElementById('gl-signin'),
      stats: (document.getElementById('stats')||{}).textContent,
      rowsRendered: document.querySelectorAll('.row').length
    })`,
  });
  console.log('page state:', res.result?.result?.value);

  // Screenshot through CDP too: --screenshot with --virtual-time-budget would
  // capture the same false "Loading…" state this probe exists to disprove.
  const fs = require('fs');
  const path = require('path');
  const shot = path.join(require('os').tmpdir(), 'deployed_gate.png');
  const img = await send('Page.captureScreenshot', { format: 'png' });
  if (img.result?.data) {
    fs.writeFileSync(shot, Buffer.from(img.result.data, 'base64'));
    console.log('screenshot:', shot, fs.statSync(shot).size, 'bytes');
  }

  ws.close();
  process.exit(0);
})().catch((e) => { console.log('ERROR:', e.message); process.exit(1); });
