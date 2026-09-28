// SPEC section 11: transcript content is untrusted, so the frontend source
// must not contain any API that parses strings as markup or code.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const STATIC_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');
const TESTS_DIR = join(STATIC_DIR, 'tests');

// Built from parts so this file does not trip its own scan.
const BANNED = [
  ['inner', 'HTML'],
  ['outer', 'HTML'],
  ['insertAdjacent', 'HTML'],
  ['document', '.write'],
  ['eval', '('],
  ['new ', 'Function'],
  ['java', 'script:'],
  ['set', 'Timeout(\''],
  ['set', 'Timeout("'],
].map((parts) => parts.join(''));

function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (path === TESTS_DIR) continue;
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if (/\.(js|html|css|svg)$/.test(name)) out.push(path);
  }
  return out;
}

test('static sources exist to be scanned', () => {
  const files = sourceFiles(STATIC_DIR).map((f) => relative(STATIC_DIR, f));
  for (const expected of ['index.html', 'app.js', 'api.js', 'dom.js', 'lib/format.js', 'lib/state.js', 'views/timeline.js']) {
    assert.ok(files.includes(expected), `missing ${expected}`);
  }
});

test('no banned markup or code-evaluation API appears in static sources', () => {
  const hits = [];
  for (const file of sourceFiles(STATIC_DIR)) {
    const text = readFileSync(file, 'utf8');
    for (const needle of BANNED) {
      if (text.includes(needle)) hits.push(`${relative(STATIC_DIR, file)}: ${needle}`);
    }
  }
  assert.deepEqual(hits, []);
});

test('only api.js talks to the server', () => {
  const offenders = sourceFiles(STATIC_DIR)
    .filter((f) => f.endsWith('.js') && !f.endsWith('api.js'))
    .filter((f) => /\bfetch\(|new EventSource\(/.test(readFileSync(f, 'utf8')))
    .map((f) => relative(STATIC_DIR, f));
  assert.deepEqual(offenders, []);
});

test('index.html loads only same-origin scripts and styles', () => {
  const html = readFileSync(join(STATIC_DIR, 'index.html'), 'utf8');
  assert.doesNotMatch(html, /(src|href)="(https?:)?\/\//, 'no external origins');
  assert.doesNotMatch(html, /<script>(?!\s*<\/script>)/, 'no inline scripts');
  assert.doesNotMatch(html, /\sstyle="/, 'no inline style attributes');
  assert.doesNotMatch(html, /\son[a-z]+="/, 'no inline event handlers');
  assert.match(html, /script-src 'self'/);
});

test('the em dash character is not used anywhere in the frontend', () => {
  const emDash = String.fromCharCode(0x2014);
  const all = [...sourceFiles(STATIC_DIR), ...readdirSync(TESTS_DIR).map((n) => join(TESTS_DIR, n))];
  const hits = all.filter((f) => readFileSync(f, 'utf8').includes(emDash)).map((f) => relative(STATIC_DIR, f));
  assert.deepEqual(hits, []);
});
