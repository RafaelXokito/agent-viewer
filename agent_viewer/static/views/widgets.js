// Small shared UI pieces built on dom.js.

import { h } from '../dom.js';
import { shortId } from '../lib/format.js';

const STATUS_LABELS = Object.freeze({
  running: 'Running',
  idle: 'Idle',
  stale: 'Stale (no activity, never finished)',
  finished: 'Finished',
});

export function statusDot(status) {
  const known = STATUS_LABELS[status] ? status : 'unknown';
  const label = STATUS_LABELS[status] || `Status: ${status || 'unknown'}`;
  return h('span', { class: `dot dot-${known}`, title: label, role: 'img', 'aria-label': label });
}

export function statusChip(status) {
  const known = STATUS_LABELS[status] ? status : 'unknown';
  return h('span', { class: `chip chip-${known}`, text: status || 'unknown' });
}

export function sourceBadge(source) {
  const label = source === 'omp' ? 'Oh My Pi' : source === 'claude' ? 'Claude Code' : String(source);
  return h('span', { class: `badge badge-${source === 'omp' ? 'omp' : 'claude'}`, title: label, text: source });
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = h('textarea', { class: 'offscreen', readonly: true });
  area.value = text;
  document.body.appendChild(area);
  area.select();
  document.execCommand('copy');
  area.remove();
}

/** A small button that copies `value` and briefly confirms. */
export function copyButton(value, label = 'Copy') {
  const btn = h('button', {
    type: 'button',
    class: 'copy-btn',
    title: `${label}: ${value}`,
    'aria-label': `${label}: ${value}`,
    text: 'copy',
    onClick: async (e) => {
      e.stopPropagation();
      try {
        await copyText(String(value));
        btn.textContent = 'copied';
      } catch {
        btn.textContent = 'failed';
      }
      setTimeout(() => {
        btn.textContent = 'copy';
      }, 1200);
    },
  });
  return btn;
}

/** Monospace id, shortened, with the full value on hover and a copy button. */
export function idChip(id, { n = 8, label = 'Copy id' } = {}) {
  const text = id === 'main' ? 'main' : shortId(id, n);
  return h('span', { class: 'id-chip' }, h('code', { class: 'mono', title: String(id), text }), copyButton(id, label));
}

export function emptyState(text) {
  return h('p', { class: 'empty', text });
}

export function errorBox(err) {
  const code = err && err.code ? `${err.code}: ` : '';
  return h('p', { class: 'error-box', role: 'alert', text: `${code}${(err && err.message) || String(err)}` });
}

export function loading(text = 'Loading...') {
  return h('p', { class: 'loading', role: 'status', text });
}
