/* 数据看板:汇总、平台对比、作品明细、趋势折线(手写内联 SVG,零构建链)。 */
import { $, $$, readJSON, h } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { initChrome } from '../ui/chrome.js';
import { api } from '../api.js';

initChrome();

const DATA = readJSON('page-data', {});
let items = DATA.items || [];
let byPlatform = DATA.by_platform || [];

const NS = 'http://www.w3.org/2000/svg';

function say(message, type = 'success') { toast(message, { type }); }

function num(value) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  if (n >= 100000) return `${(n / 10000).toFixed(1)} 万`;
  if (n >= 10000) return `${(n / 10000).toFixed(2)} 万`;
  return String(n);
}

function rawNum(value) {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString('zh-CN');
}

/* ---------------------------------------------------------------------------
 * 内联 SVG 折线图
 * --------------------------------------------------------------------------- */
function svgEl(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

/**
 * 折线图。**缺失值断线,不掉到 0** —— 「取不到」和「真的是 0」是两回事,
 * 连成一条线会把前者画成后者。
 */
function lineChart(points, { width = 720, height = 200, field = 'views' } = {}) {
  const pad = { l: 48, r: 16, t: 16, b: 26 };
  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart-svg', role: 'img' });
  const vals = points.map((p) => p[field]).filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) {
    const text = svgEl('text', { x: width / 2, y: height / 2, 'text-anchor': 'middle', class: 'chart-empty' });
    text.textContent = vals.length ? '只有一个数据点,画不出趋势' : '还没有抓到数据';
    svg.appendChild(text);
    return svg;
  }
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = (max - min) || 1;
  const x = (i) => pad.l + (i * (width - pad.l - pad.r)) / Math.max(1, points.length - 1);
  const y = (v) => height - pad.b - ((v - min) / span) * (height - pad.t - pad.b);

  // 网格与刻度
  for (let g = 0; g <= 3; g++) {
    const value = min + (span * g) / 3;
    const gy = y(value);
    svg.appendChild(svgEl('line', { x1: pad.l, y1: gy, x2: width - pad.r, y2: gy, class: 'chart-grid' }));
    const label = svgEl('text', { x: pad.l - 8, y: gy + 4, 'text-anchor': 'end', class: 'chart-label' });
    label.textContent = num(Math.round(value));
    svg.appendChild(label);
  }

  // 分段:连续的非空值才连起来
  let segment = [];
  const flush = () => {
    if (segment.length > 1) {
      const d = segment.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ');
      svg.appendChild(svgEl('path', { d, class: 'chart-line' }));
    }
    segment = [];
  };
  points.forEach((p, i) => {
    const v = p[field];
    if (v === null || v === undefined) { flush(); return; }
    segment.push([x(i), y(v)]);
    svg.appendChild(svgEl('circle', { cx: x(i), cy: y(v), r: 2.5, class: 'chart-dot' }));
  });
  flush();

  // 时间轴
  const first = points[0];
  const last = points[points.length - 1];
  const fmt = (ts) => {
    const d = new Date(Number(ts) * 1000);
    return `${d.getMonth() + 1}/${d.getDate()}`;
  };
  const l1 = svgEl('text', { x: pad.l, y: height - 8, class: 'chart-label' });
  l1.textContent = fmt(first.captured_at);
  const l2 = svgEl('text', { x: width - pad.r, y: height - 8, 'text-anchor': 'end', class: 'chart-label' });
  l2.textContent = fmt(last.captured_at);
  svg.appendChild(l1);
  svg.appendChild(l2);
  return svg;
}

/* ---------------------------------------------------------------------------
 * 渲染
 * --------------------------------------------------------------------------- */
function renderKpi() {
  const published = items.filter((i) => i.state === 'published' || i.state === 'submitted');
  const sum = (key) => published.reduce((acc, i) => acc + (Number(i.metrics?.[key]) || 0), 0);
  const views = sum('views');
  const likes = sum('likes');
  const eng = published.reduce((acc, i) => {
    const v = Number(i.metrics?.views) || 0;
    if (!v || i.engagement_rate === null || i.engagement_rate === undefined) return acc;
    return acc + i.engagement_rate * v;
  }, 0);
  $('#kpi-count').textContent = String(published.length);
  $('#kpi-views').textContent = num(views);
  $('#kpi-likes').textContent = num(likes);
  $('#kpi-rate').textContent = views ? `${((eng / views) * 100).toFixed(2)}%` : '—';
}

function renderPlatforms() {
  const tbody = $('#platform-table tbody');
  tbody.innerHTML = '';
  if (!byPlatform.length) {
    tbody.appendChild(h('tr', {}, h('td', { colspan: 8, class: 'subtle' }, '还没有发布记录')));
    return;
  }
  for (const p of byPlatform) {
    tbody.appendChild(h('tr', {},
      h('td', {}, h('span', { class: 'badge tone-brand' }, p.label || p.platform)),
      h('td', { class: 'num' }, String(p.count || 0)),
      h('td', { class: 'num' }, String(p.published || 0)),
      h('td', { class: 'num' }, num(p.views)),
      h('td', { class: 'num' }, num(p.likes)),
      h('td', { class: 'num' }, num(p.comments)),
      h('td', { class: 'num' }, num(p.favorites)),
      h('td', { class: 'num' }, num(p.shares))));
  }
}

function renderWorks() {
  const tbody = $('#work-table tbody');
  tbody.innerHTML = '';
  if (!items.length) {
    tbody.appendChild(h('tr', {}, h('td', { colspan: 10, class: 'subtle' }, '还没有发布过的作品')));
    return;
  }
  const maxViews = Math.max(1, ...items.map((i) => Number(i.metrics?.views) || 0));
  for (const item of items) {
    const views = Number(item.metrics?.views) || 0;
    const bar = h('div', { class: 'spark' },
      h('span', { style: `width:${Math.max(2, (views / maxViews) * 100)}%` }));
    const tr = h('tr', { 'data-pub': item.id, class: 'is-clickable' },
      h('td', {}, h('span', { class: 'badge tone-brand' }, item.platform_label)),
      h('td', { class: 'pub-title', title: item.title }, item.title),
      h('td', { class: 'num' }, num(item.metrics?.views)),
      h('td', { class: 'num' }, num(item.metrics?.likes)),
      h('td', { class: 'num' }, num(item.metrics?.comments)),
      h('td', { class: 'num' }, num(item.metrics?.favorites)),
      h('td', { class: 'num' }, num(item.metrics?.shares)),
      h('td', { class: 'num' }, num(item.metrics?.followers)),
      h('td', { class: 'num' },
        item.engagement_rate === null || item.engagement_rate === undefined
          ? '—' : `${(item.engagement_rate * 100).toFixed(2)}%`),
      h('td', {}, bar));
    tbody.appendChild(tr);
  }
}

async function openTrend(pubId) {
  const card = $('#trend-card');
  const box = $('#trend-chart');
  const item = items.find((i) => i.id === pubId);
  $('#trend-title').textContent = item ? `${item.platform_label} · ${item.title}` : '趋势';
  card.hidden = false;
  box.innerHTML = '';
  try {
    const data = await api.get(`/api/metrics/publications/${pubId}`);
    const points = data.trend || [];
    box.appendChild(lineChart(points, { field: 'views' }));
    $('#trend-note').textContent = points.length
      ? `共 ${points.length} 次抓取。缺失的抓取会断线,不画成 0。`
      : '还没有抓到数据 —— 定时任务每 30 分钟跑一次,发布后过一会儿就有了。';
  } catch (err) {
    say(err.message, 'danger');
  }
}

$('#work-table')?.addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-pub]');
  if (tr) openTrend(tr.dataset.pub);
});
$('#trend-close')?.addEventListener('click', () => { $('#trend-card').hidden = true; });

async function reload() {
  try {
    const data = await api.get('/api/metrics/overview');
    items = data.items || [];
    byPlatform = data.by_platform || [];
    renderKpi();
    renderPlatforms();
    renderWorks();
  } catch (err) {
    say(err.message, 'danger');
  }
}

$('#btn-refresh')?.addEventListener('click', reload);

renderKpi();
renderPlatforms();
renderWorks();
setInterval(reload, 60000);
