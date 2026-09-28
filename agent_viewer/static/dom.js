// Safe element builder: the only DOM construction helper.
// Text always goes in through textContent or text nodes, never as markup.
// URL-bearing attributes are refused except in-app "#..." links, and string
// event handlers are refused, so transcript data cannot become code.

const URL_ATTRS = new Set([
  'src', 'srcset', 'srcdoc', 'action', 'formaction', 'xlink:href', 'poster',
  'data', 'background', 'cite', 'codebase', 'manifest', 'ping', 'style',
]);
const PROPERTY_ATTRS = new Set(['value', 'checked', 'selected', 'disabled', 'open']);

function setProp(el, key, value) {
  if (value == null) return;
  if (key === 'text') {
    el.textContent = String(value);
    return;
  }
  if (key === 'class') {
    if (!value) return;
    el.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
    return;
  }
  if (key === 'dataset') {
    for (const [k, v] of Object.entries(value)) {
      if (v != null) el.dataset[k] = String(v);
    }
    return;
  }
  if (key.startsWith('on')) {
    if (typeof value !== 'function') throw new TypeError(`handler ${key} must be a function`);
    el.addEventListener(key.slice(2).toLowerCase(), value);
    return;
  }
  if (PROPERTY_ATTRS.has(key)) {
    el[key] = value;
    return;
  }
  if (key === 'href') {
    const href = String(value);
    if (!href.startsWith('#')) throw new TypeError('only in-app "#" links are allowed');
    el.setAttribute('href', href);
    return;
  }
  if (URL_ATTRS.has(key.toLowerCase())) throw new TypeError(`attribute ${key} is not allowed`);
  if (value === false) return;
  el.setAttribute(key, value === true ? '' : String(value));
}

/** Append children: strings become text nodes, null and false are skipped. */
export function append(parent, children) {
  for (const child of children) {
    if (child == null || child === false) continue;
    if (Array.isArray(child)) {
      append(parent, child);
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }
  return parent;
}

/** h('div', {class: 'x', onClick: fn}, 'text', childNode, [more]) */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) setProp(el, key, value);
  }
  return append(el, children);
}

/** Replace all children of `el`. */
export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

/** Set a CSS custom property through the CSSOM (allowed by the CSP). */
export function setVar(el, name, value) {
  el.style.setProperty(name, String(value));
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * svg('path', {class: 'edge', d: '...'}): the SVG twin of `h`, with the same
 * refusals. `class` goes through setAttribute because SVG className is read-only.
 */
export function svg(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') {
        const cls = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
        if (cls) el.setAttribute('class', String(cls));
      } else {
        setProp(el, key, value);
      }
    }
  }
  return append(el, children);
}
