/* DOM 小工具。零依赖,三个页面共用。 */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 读模板里 <script type="application/json"> 注入的数据。 */
export function readJSON(id, fallback = {}) {
  const el = document.getElementById(id);
  if (!el) return fallback;
  try { return JSON.parse(el.textContent); } catch { return fallback; }
}

/** 引用 _sprite.html 里的图标。 */
export function icon(name, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', `i ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(ns, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

/** 极简元素工厂:h('div', {class: 'x'}, '文本', node)。字符串一律当文本,不会被解析成 HTML。 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return el;
}

/** 按钮执行异步操作期间显示加载态并禁用,防止重复提交。 */
export async function withLoading(btn, fn) {
  if (!btn) return fn();
  if (btn.classList.contains('is-loading')) return undefined;
  btn.classList.add('is-loading');
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.classList.remove('is-loading');
    btn.disabled = false;
  }
}

/** 秒级时间戳 → "3 分钟前"。 */
export function relTime(ts) {
  const diff = Date.now() / 1000 - Number(ts);
  if (!Number.isFinite(diff)) return '';
  if (diff < 45) return '刚刚';
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)} 天前`;
  const d = new Date(Number(ts) * 1000);
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

export function debounce(fn, ms = 150) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
