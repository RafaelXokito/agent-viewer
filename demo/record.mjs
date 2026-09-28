// Records the README demo: builds synthetic transcripts (demo/demo_data.py),
// starts agent-viewer on them, drives headless Chrome over CDP with a visible
// cursor, captures a screencast, and encodes docs/demo.gif with ffmpeg.
//
//   node demo/record.mjs [--port 8799] [--out docs/demo.gif]
//
// Needs python3, google-chrome (or CHROME=/path/to/chrome), ffmpeg and Node 22+.

import { spawn, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : fallback;
};
const PORT = Number(arg('--port', '8799'));
const OUT = resolve(REPO, arg('--out', 'docs/demo.gif'));
const CDP_PORT = 9355;
const WIDTH = 1280;
const HEIGHT = 720;
const BASE = `http://127.0.0.1:${PORT}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const work = mkdtempSync(join(tmpdir(), 'agent-viewer-demo-'));
const claudeRoot = join(work, 'claude');
const ompRoot = join(work, 'omp');
const framesDir = join(work, 'frames');
[claudeRoot, ompRoot, framesDir].forEach((d) => mkdirSync(d, { recursive: true }));
const children = [];

function run(cmd, args) {
  const child = spawn(cmd, args, { cwd: REPO, stdio: 'ignore' });
  children.push(child);
  return child;
}

async function waitFor(url) {
  for (let i = 0; i < 100; i += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch { /* not up yet */ }
    await sleep(100);
  }
  throw new Error(`timed out waiting for ${url}`);
}

// ---- CDP ----------------------------------------------------------------------------

let ws;
let nextId = 0;
const pending = new Map();
const frames = [];

async function connect() {
  let targets = [];
  for (let i = 0; i < 50 && !targets.length; i += 1) {
    try { targets = (await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()).filter((t) => t.type === 'page'); } catch { /* starting */ }
    if (!targets.length) await sleep(200);
  }
  ws = new WebSocket(targets[0].webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r));
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m.result);
      pending.delete(m.id);
    } else if (m.method === 'Page.screencastFrame') {
      frames.push({ data: m.params.data, t: m.params.metadata.timestamp });
      send('Page.screencastFrameAck', { sessionId: m.params.sessionId });
    } else if (m.method === 'Runtime.exceptionThrown') {
      console.error('page error:', m.params.exceptionDetails.text, m.params.exceptionDetails.exception?.description || '');
    } else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      console.error('page console:', m.params.args.map((a) => a.value ?? a.description).join(' '));
    } else if (m.method === 'Log.entryAdded') {
      console.error('page log:', m.params.entry.level, m.params.entry.text, m.params.entry.url || '');
    } else if (m.method === 'Network.loadingFailed') {
      console.error('request failed:', m.params.errorText, m.params.blockedReason || '');
    }
  });
}

function send(method, params = {}) {
  return new Promise((r) => {
    nextId += 1;
    pending.set(nextId, r);
    ws.send(JSON.stringify({ id: nextId, method, params }));
  });
}

async function evaluate(expression) {
  const res = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  return res && res.result ? res.result.value : undefined;
}

// ---- cursor and interactions ------------------------------------------------------------

// A fake pointer so viewers can follow the clicks; styled through CSSOM, which the page's CSP allows.
const CURSOR_JS = `(() => {
  if (document.getElementById('demo-cursor')) return;
  const c = document.createElement('div');
  c.id = 'demo-cursor';
  Object.assign(c.style, { position: 'fixed', left: '0', top: '0', width: '22px', height: '22px', marginLeft: '-11px', marginTop: '-11px',
    borderRadius: '50%', background: 'rgba(255,255,255,0.35)', border: '2px solid #fff', boxShadow: '0 0 0 2px rgba(0,0,0,0.45)',
    zIndex: '99999', pointerEvents: 'none', transform: 'translate(640px, 360px)', transition: 'transform 0.7s ease-in-out, scale 0.15s' });
  document.body.appendChild(c);
})()`;

async function moveTo(x, y) {
  await evaluate(`document.getElementById('demo-cursor').style.transform = 'translate(${x}px, ${y}px)'`);
  await sleep(750);
}

/** Center of the first element matching `selector` whose text includes `text`, waiting up to 5 s for it. */
async function centerOf(selector, text = '') {
  const find = `(() => {
    const el = [...document.querySelectorAll(${JSON.stringify(selector)})].find((e) => e.textContent.includes(${JSON.stringify(text)}));
    if (!el) return null;
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + Math.min(r.width / 2, 120)), y: Math.round(r.top + r.height / 2) };
  })()`;
  for (let i = 0; i < 50; i += 1) {
    const pos = await evaluate(find);
    if (pos) return pos;
    await sleep(100);
  }
  const page = await evaluate('location.href + " | links: " + JSON.stringify([...document.querySelectorAll("a")].map((a) => a.textContent)) + " | rows: " + document.querySelectorAll("tbody tr").length + " | " + (document.querySelector(".list-view") || {}).textContent?.slice(0, 400)');
  throw new Error(`no element ${selector} ${text}; page: ${page}`);
}

async function click(selector, text = '') {
  const { x, y } = await centerOf(selector, text);
  await moveTo(x, y);
  await evaluate(`document.getElementById('demo-cursor').style.scale = '0.7'`);
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
  }
  await sleep(150);
  await evaluate(`document.getElementById('demo-cursor').style.scale = '1'`);
}

// ---- the scenario ------------------------------------------------------------------------

async function scenario() {
  await send('Page.navigate', { url: `${BASE}/` });
  await sleep(1500);
  await evaluate(CURSOR_JS);
  await sleep(2000);

  await click('a', 'Add discount codes to checkout');
  await sleep(1200);
  await evaluate(CURSOR_JS);
  await sleep(1500);
  await click('.tree-node', 'Write discount tests');
  await sleep(2200);
  await click('.tree-node', 'Run the test suite');
  await sleep(2200);

  await click('button', 'Graph');
  await sleep(800);
  const play = run('python3', ['demo/demo_data.py', 'play', claudeRoot]);
  // Timed against demo_data.py play: main's `npm test` is pending from about 3.5 s to 7.5 s,
  // and the reviewer agent appears at about 8.5 s and works until about 15 s.
  await sleep(1500);
  await click('.graph-toolbar button', 'Fit');
  await sleep(1200);
  await click('.gpill[data-id$=":main|Bash"]');
  await sleep(3500);
  await click('.drawer-close');
  // New cards never move the viewport, so refit once the reviewer exists to keep its live calls in view.
  await centerOf('.gcard', 'Review the diff');
  await click('.graph-toolbar button', 'Fit');
  await moveTo(WIDTH - 60, HEIGHT - 120);
  await new Promise((r) => (play.exitCode === null ? play.on('exit', r) : r()));
  await sleep(500);
  await click('.graph-toolbar button', 'Fit');
  await moveTo(WIDTH - 60, HEIGHT - 120);
  await sleep(3000);
}

// ---- encoding -------------------------------------------------------------------------------

function encode() {
  const lines = ['ffconcat version 1.0'];
  frames.forEach((f, i) => {
    const file = join(framesDir, `f${String(i).padStart(5, '0')}.jpg`);
    writeFileSync(file, Buffer.from(f.data, 'base64'));
    const next = frames[i + 1] ? frames[i + 1].t : f.t + 1;
    lines.push(`file '${file}'`, `duration ${(next - f.t).toFixed(3)}`);
  });
  lines.push(`file '${join(framesDir, `f${String(frames.length - 1).padStart(5, '0')}.jpg`)}'`);
  const list = join(work, 'frames.ffconcat');
  writeFileSync(list, `${lines.join('\n')}\n`);
  const filter = 'fps=12,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];'
    + '[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle';
  mkdirSync(dirname(OUT), { recursive: true });
  const res = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', list, '-vf', filter, '-loop', '0', OUT],
    { stdio: 'inherit' });
  if (res.status !== 0) throw new Error('ffmpeg failed');
}

async function main() {
  const built = spawnSync('python3', ['demo/demo_data.py', 'build', claudeRoot], { cwd: REPO, stdio: 'inherit' });
  if (built.status !== 0) throw new Error('building demo data failed');
  run('python3', ['-m', 'agent_viewer', '--port', String(PORT), '--claude-root', claudeRoot, '--omp-root', ompRoot]);
  await waitFor(`${BASE}/api/sessions`);
  for (let i = 0; i < 100; i += 1) {
    const list = await (await fetch(`${BASE}/api/sessions?limit=10`)).json();
    if (list.items && list.items.length === 3) break;
    await sleep(100);
  }
  run(process.env.CHROME || 'google-chrome', ['--headless=new', `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${join(work, 'chrome')}`, '--no-first-run', '--hide-scrollbars', `--window-size=${WIDTH},${HEIGHT}`,
    '--force-dark-mode', 'about:blank']);
  await connect();
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Log.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false });
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
  await send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: WIDTH, maxHeight: HEIGHT, everyNthFrame: 1 });
  await scenario();
  await send('Page.stopScreencast');
  console.log(`captured ${frames.length} frames over ${(frames.at(-1).t - frames[0].t).toFixed(1)} s`);
  encode();
  console.log(`wrote ${OUT}`);
}

try {
  await main();
} finally {
  children.forEach((c) => c.kill());
  rmSync(work, { recursive: true, force: true });
}
process.exit(0);
