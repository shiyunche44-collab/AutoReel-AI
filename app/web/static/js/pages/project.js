/* 工作台:Tabs、门禁操作、脚本编辑、渲染看板、状态轮询。 */
import { $, $$, readJSON, withLoading, debounce, h, icon } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { confirmDialog, openModal } from '../ui/modal.js';
import { initChrome, paintBadge, stateMeta } from '../ui/chrome.js';
import { api } from '../api.js';
import { initConceptStyle } from './concept-style.js';

initChrome();

const DATA = readJSON('page-data', {});
const PID = DATA.pid;
const RESTORE_KEY = `aimovice.restore.${PID}`;
let lastState = DATA.state;
let lastSucceeded = DATA.succeeded || 0;
// 不调视频生成,一拍 = 一段旁白;门禁 B 整片一次审完
const UNIT = '拍';

// ---------------------------------------------------------------------------
// Tabs:hash 记住当前页,刷新后不丢
// ---------------------------------------------------------------------------
const content = $('#content');
const tabs = $$('.tab', content);
const panels = $$('.tab-panel', content);

function showTab(name, { push = true } = {}) {
  if (!tabs.some((t) => t.dataset.tab === name)) name = 'overview';
  for (const t of tabs) {
    const on = t.dataset.tab === name;
    t.setAttribute('aria-selected', String(on));
    t.tabIndex = on ? 0 : -1;
  }
  for (const p of panels) p.classList.toggle('is-active', p.dataset.panel === name);
  if (push) history.replaceState(null, '', `#${name}`);
  if (name === 'script') autosizeAll();
  if (name === 'board') observeVideos();
}

tabs.forEach((t, i) => {
  t.addEventListener('click', () => showTab(t.dataset.tab));
  t.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    next.focus();
    showTab(next.dataset.tab);
  });
});

function gotoTab(name) {
  showTab(name);
  content.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$$('[data-goto]').forEach((b) => b.addEventListener('click', () => gotoTab(b.dataset.goto)));
window.addEventListener('hashchange', () => showTab(location.hash.slice(1), { push: false }));

// 刷新后恢复滚动位置与 Tab(状态跃迁触发的自动刷新)
function restoreView() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(RESTORE_KEY) || 'null'); sessionStorage.removeItem(RESTORE_KEY); } catch { /* 忽略 */ }
  const fromHash = location.hash.slice(1);
  showTab(fromHash || (saved && saved.tab) || content.dataset.defaultTab, { push: !!fromHash });
  if (saved && typeof saved.y === 'number') requestAnimationFrame(() => window.scrollTo(0, saved.y));
}

function reloadPreserving() {
  const active = tabs.find((t) => t.getAttribute('aria-selected') === 'true');
  try { sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ tab: active && active.dataset.tab, y: window.scrollY })); } catch { /* 忽略 */ }
  location.reload();
}

// ---------------------------------------------------------------------------
// 推进 / 门禁
// ---------------------------------------------------------------------------
async function act(btn, url, body, label) {
  await withLoading(btn, async () => {
    try {
      const data = await api.post(url, body);
      if (data.state && data.state !== lastState) {
        toast(`${label}完成 · ${stateMeta(data.state).label}`);
        lastState = data.state;
        setTimeout(reloadPreserving, 700);
      } else {
        toast(data.reason || (data.waiting ? '正在等待确认' : `${label}完成`), { type: 'brand' });
        refreshStatus();
      }
    } catch (err) {
      toast(err.message, { type: 'danger', title: `${label}失败` });
    }
  });
}

const btnAdvance = $('#btn-advance');
if (btnAdvance) btnAdvance.addEventListener('click', () => act(btnAdvance, `/api/projects/${PID}/advance`, undefined, '推进'));

// 概念动画风格面板(本片有概念拍时才显示);门禁 B 上换风格会退回渲染,刷新页面跟上状态
const conceptStyle = initConceptStyle(PID, { onRerender: () => setTimeout(reloadPreserving, 700) });

const btnGateA = $('#btn-gate-a');
if (btnGateA) {
  btnGateA.addEventListener('click', async () => {
    if (scriptDirty()) {
      toast('脚本还有未保存的修改,请先保存再确认', { type: 'warning' });
      gotoTab('script');
      return;
    }
    const ok = await confirmDialog({
      title: '确认脚本与分镜?',
      body: h('div', {},
        h('p', {}, '确认后先合成全片旁白,再按旁白时长逐拍渲染,不调用视频生成。'),
        h('p', {}, `本项目共 ${DATA.shot_count} 个${UNIT},确认后脚本与分镜将不能再修改。`),
        conceptStyle && conceptStyle.hasConcepts()
          ? h('p', {}, `概念动画风格:${conceptStyle.currentName()}(审片时还能换,只重渲概念动画那几拍)`) : null),
      confirmText: '确认并开始渲染',
      tone: 'success',
    });
    if (ok) await act(btnGateA, `/api/projects/${PID}/gate`, { gate: 'A' }, '确认 A');
  });
}

const btnGateB = $('#btn-gate-b');
if (btnGateB) {
  // 草稿就是成片,没有第二遍生成,也就没有分批放行
  $('#gate-hint').textContent = `共 ${DATA.total || DATA.shot_count} 拍。不满意的拍可以单独「重拍」换一组背景,全部满意后确认,直接拼接混音出成片。`;
  btnGateB.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: '各拍都审过了?',
      body: '确认后把这些拍直接拼接、混入旁白与 BGM,合成成片。不调用视频生成。',
      confirmText: '确认并合成',
      tone: 'success',
    });
    if (ok) await act(btnGateB, `/api/projects/${PID}/gate`, { gate: 'B' }, '确认 B');
  });
}

// ---------------------------------------------------------------------------
// 脚本编辑
// ---------------------------------------------------------------------------
const scriptRows = $$('.script-row');
const saveBtn = $('#btn-save-script');
const savebar = $('#savebar');
const original = new Map(scriptRows.map((r) => [r, r.querySelector('.script-text').value]));

function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = `${ta.scrollHeight + 2}px`;
}
function autosizeAll() { scriptRows.forEach((r) => autosize(r.querySelector('.script-text'))); }

function updateCounts() {
  let total = 0;
  for (const r of scriptRows) {
    const n = r.querySelector('.script-text').value.trim().length;
    total += n;
    const c = r.querySelector('.script-count');
    c.textContent = `${n} 字`;
    // 口播约 4–5 字/秒,单段太长会压不进镜头时长
    c.classList.toggle('is-long', n > 60);
  }
  const t = $('#script-total');
  if (t) t.textContent = scriptRows.length ? `共 ${total} 字 · 约 ${Math.round(total / 4.5)} 秒口播` : '';
}

function scriptDirty() {
  return scriptRows.some((r) => r.querySelector('.script-text').value !== original.get(r));
}

function setStatus(text, iconName) {
  const s = $('#save-status');
  if (!s) return;
  s.replaceChildren(...(iconName ? [icon(iconName, 'i-sm')] : []), text);
}

function updateDirty() {
  let n = 0;
  for (const r of scriptRows) {
    const d = r.querySelector('.script-text').value !== original.get(r);
    r.classList.toggle('is-dirty', d);
    if (d) n++;
  }
  if (!savebar || !DATA.editable) return;
  savebar.classList.toggle('is-dirty', n > 0);
  setStatus(n ? `${n} 段有未保存的修改` : '所有修改已保存', n ? null : 'check');
}

async function saveScript() {
  if (!saveBtn || saveBtn.disabled) return;
  const segments = scriptRows.map((row) => ({
    idx: parseInt(row.dataset.idx, 10),
    section: row.querySelector('.script-section').textContent.trim(),
    text: row.querySelector('.script-text').value.trim(),
    fact_refs: $$('.script-refs code', row).map((c) => c.textContent.trim()),
  }));
  await withLoading(saveBtn, async () => {
    try {
      const data = await api.patch(`/api/projects/${PID}/script`, { segments });
      scriptRows.forEach((r) => original.set(r, r.querySelector('.script-text').value));
      updateDirty();
      toast(`已保存 ${data.segments} 段脚本`);
    } catch (err) {
      toast(err.message, { type: 'danger', title: '保存失败' });
    }
  });
}

if (scriptRows.length) {
  scriptRows.forEach((r) => {
    const ta = r.querySelector('.script-text');
    ta.addEventListener('input', () => { autosize(ta); updateCounts(); updateDirty(); });
  });
  updateCounts();
  window.addEventListener('resize', debounce(autosizeAll, 150));
  if (saveBtn) saveBtn.addEventListener('click', saveScript);
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && DATA.editable) {
      e.preventDefault();
      if (scriptDirty()) saveScript();
      else toast('没有需要保存的修改', { type: 'brand', duration: 1800 });
    }
  });
  window.addEventListener('beforeunload', (e) => {
    if (scriptDirty()) { e.preventDefault(); e.returnValue = ''; }
  });
}

// 点击脚本里的事实编号 → 跳到对应事实并高亮
document.addEventListener('click', (e) => {
  const ref = e.target.closest('.js-fact-ref');
  if (!ref) return;
  const target = document.getElementById(`fact-${ref.dataset.ref}`);
  if (!target) { toast(`找不到事实 ${ref.dataset.ref}`, { type: 'warning' }); return; }
  resetFactFilters();
  showTab('facts');
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.classList.remove('is-flash');
  void target.offsetWidth;
  target.classList.add('is-flash');
});

// ---------------------------------------------------------------------------
// 事实筛选
// ---------------------------------------------------------------------------
let factScope = 'all';
let factQuery = '';
function applyFactFilters() {
  let n = 0;
  for (const f of $$('#fact-list .fact')) {
    const show = (factScope === 'all' || f.dataset.scope === factScope) && (!factQuery || f.dataset.text.includes(factQuery));
    f.classList.toggle('hidden', !show);
    if (show) n++;
  }
  const empty = $('#fact-empty');
  if (empty) empty.classList.toggle('hidden', n > 0);
}
function resetFactFilters() {
  factScope = 'all';
  factQuery = '';
  const s = $('#fact-search');
  if (s) s.value = '';
  $$('#fact-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.scope === 'all')));
  applyFactFilters();
}
$$('#fact-filters .chip').forEach((chip) => chip.addEventListener('click', () => {
  factScope = chip.dataset.scope;
  $$('#fact-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
  applyFactFilters();
}));
const factSearch = $('#fact-search');
if (factSearch) factSearch.addEventListener('input', debounce(() => { factQuery = factSearch.value.trim().toLowerCase(); applyFactFilters(); }, 120));

// ---------------------------------------------------------------------------
// 渲染看板
// ---------------------------------------------------------------------------
const LIVE = new Set(['submitted', 'queued', 'running']);
const shots = $$('#shot-grid .shot');
let shotFilter = 'all';

function shotMatches(s, f) {
  const st = s.dataset.status;
  if (f === 'all') return true;
  if (f === 'live') return LIVE.has(st);
  return st === f;
}
function applyShotFilter() {
  let n = 0;
  for (const s of shots) {
    const show = shotMatches(s, shotFilter);
    s.classList.toggle('hidden', !show);
    if (show) n++;
  }
  const empty = $('#shot-empty');
  if (empty) empty.classList.toggle('hidden', n > 0);
}
function countShots() {
  for (const f of ['live', 'succeeded', 'failed', 'pending']) {
    const el = $(`[data-c="${f}"]`);
    if (el) el.textContent = shots.filter((s) => shotMatches(s, f)).length;
  }
}
$$('#shot-filters .chip').forEach((chip) => chip.addEventListener('click', () => {
  shotFilter = chip.dataset.f;
  $$('#shot-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
  applyShotFilter();
}));
countShots();

// 视频只在进入视口时加载:40 个镜头同时 autoplay 会把浏览器拖垮。
// 能悬停的设备只显示首帧、悬停才播;触屏没有悬停,进入视口就播。
const canHover = matchMedia('(hover: hover)').matches;
let io = null;
// 成片合成后 shots/ 里的单拍会被清掉,只剩草稿:成片地址 404 时退回草稿
function withFallback(v) {
  v.addEventListener('error', () => {
    const fb = v.dataset.fallback;
    if (!fb || v.dataset.src === fb) return;
    v.dataset.src = fb;
    const wasPlaying = v.closest('.shot.is-playing');
    v.src = v.src.includes('#t=') ? `${fb}#t=0.1` : fb;
    if (wasPlaying || v.autoplay) v.play().catch(() => {});
  });
  return v;
}
function loadVideo(v) {
  if (v.src) return;
  withFallback(v);
  v.preload = 'metadata';
  // #t=0.1 让浏览器解出第一帧当封面,不然是黑块
  v.src = `${v.dataset.src}#t=0.1`;
}
function observeVideos() {
  if (io || !shots.length) return;
  const vids = $$('#shot-grid video[data-src]');
  if (canHover) {
    for (const v of vids) {
      const shot = v.closest('.shot');
      const play = () => { loadVideo(v); shot.classList.add('is-playing'); v.play().catch(() => {}); };
      const stop = () => { shot.classList.remove('is-playing'); v.pause(); };
      shot.addEventListener('mouseenter', play);
      shot.addEventListener('mouseleave', stop);
      shot.addEventListener('focus', play);
      shot.addEventListener('blur', stop);
    }
  }
  if (!('IntersectionObserver' in window)) {
    vids.forEach((v) => { loadVideo(v); if (!canHover) v.play().catch(() => {}); });
    return;
  }
  io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      const v = en.target;
      if (en.isIntersecting) {
        loadVideo(v);
        if (!canHover) v.play().catch(() => {});
      } else if (!v.paused) {
        v.pause();
      }
    }
  }, { rootMargin: '120px' });
  vids.forEach((v) => io.observe(v));
}

let lightboxOpen = false;
function openShot(idx) {
  const visible = shots.filter((s) => !s.classList.contains('hidden'));
  let pos = visible.findIndex((s) => s.dataset.idx === String(idx));
  if (pos < 0) return;
  lightboxOpen = true;

  const media = h('div', { class: 'lb-media' });
  const side = h('div', { class: 'lb-body' });
  const prev = h('button', { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '上一个镜头' }, icon('chevron-left'));
  const next = h('button', { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '下一个镜头' }, icon('chevron-right'));
  const closeBtn = h('button', { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '关闭' }, icon('x'));
  const counter = h('span', { class: 'subtle small num' });
  const wrap = h('div', { class: 'lightbox' }, media,
    h('div', { class: 'lb-side' },
      h('div', { class: 'modal-head' }, counter, h('div', { class: 'lb-nav' }, prev, next, closeBtn)),
      side));

  function render() {
    const card = visible[pos];
    const v = card.querySelector('video[data-src]');
    media.replaceChildren(v
      ? withFallback(h('video', { src: v.dataset.src, 'data-fallback': v.dataset.fallback || '', 'data-src': v.dataset.src,
                                  controls: true, autoplay: true, muted: true, loop: true, playsinline: true }))
      : card.querySelector('.shot-ph').cloneNode(true));
    side.replaceChildren(card.querySelector('template.shot-detail').content.cloneNode(true));
    counter.textContent = `${pos + 1} / ${visible.length}`;
    prev.disabled = pos === 0;
    next.disabled = pos === visible.length - 1;
  }
  function move(d) {
    const np = pos + d;
    if (np < 0 || np >= visible.length) return;
    pos = np;
    render();
  }
  function onKey(e) {
    if (e.key === 'ArrowLeft') move(-1);
    if (e.key === 'ArrowRight') move(1);
  }

  const { el, close } = openModal(wrap, {
    size: 'modal-lg', label: '镜头详情',
    onClose: () => { lightboxOpen = false; document.removeEventListener('keydown', onKey); },
  });
  prev.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  closeBtn.addEventListener('click', () => close());
  document.addEventListener('keydown', onKey);
  el.addEventListener('click', async (e) => {
    const btn = e.target.closest('.js-retry');
    if (btn) { await retryShot(btn); }
  });
  render();
}

shots.forEach((s) => {
  s.addEventListener('click', () => openShot(s.dataset.idx));
  s.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openShot(s.dataset.idx); } });
});

async function retryShot(btn) {
  const idx = btn.dataset.idx;
  const ok = await confirmDialog({
    title: `重拍${UNIT} #${String(idx).padStart(2, '0')}?`,
    body: '会给这一拍换一组背景画面重新渲染,叠加层与旁白不变,不消耗生成额度。',
    confirmText: '重拍',
    tone: 'danger',
  });
  if (!ok) return;
  await withLoading(btn, async () => {
    try {
      await api.post(`/api/projects/${PID}/shots/${idx}/retry`, { stage: 'final' });
      toast(`${UNIT} #${idx} 已重新排队`);
      setTimeout(reloadPreserving, 700);
    } catch (err) {
      toast(err.message, { type: 'danger', title: '重拍失败' });
    }
  });
}

// ---------------------------------------------------------------------------
// 成片
// ---------------------------------------------------------------------------
const copyBtn = $('#btn-copy-link');
if (copyBtn) {
  copyBtn.addEventListener('click', async () => {
    const url = `${location.origin}/api/projects/${PID}/download`;
    try {
      await navigator.clipboard.writeText(url);
      toast('下载链接已复制');
    } catch {
      toast(url, { type: 'brand', title: '复制失败,请手动复制', duration: 0 });
    }
  });
}

// ---------------------------------------------------------------------------
// 状态轮询:就地刷新徽标、用量、进度;状态跃迁时才整页刷新
// ---------------------------------------------------------------------------
const POLL_MS = 5000;
let pollTimer = null;
let pendingReload = false;

async function refreshStatus() {
  try {
    const s = await api.get(`/api/projects/${PID}/status`);

    paintBadge($('#state-badge'), s.state);

    const spent = $('#spent');
    if (spent && typeof s.spent_cost === 'number') spent.textContent = s.spent_cost.toFixed(2);
    const bar = $('#usage-bar');
    if (bar && s.quota_budget) {
      const r = s.spent_cost / s.quota_budget;
      bar.firstElementChild.style.width = `${Math.min(100, r * 100)}%`;
      bar.classList.remove('tone-brand', 'tone-warning', 'tone-danger');
      bar.classList.add(r >= 1 ? 'tone-danger' : r >= 0.8 ? 'tone-warning' : 'tone-brand');
    }

    const c = s.counts || {};
    const done = c.succeeded || 0;
    const summary = $('#gen-summary');
    if (summary && s.total) {
      const parts = [`${done} / ${s.total} ${UNIT}完成`];
      const live = (c.submitted || 0) + (c.queued || 0) + (c.running || 0);
      if (live) parts.push(`${live} 个渲染中`);
      if (c.failed) parts.push(`${c.failed} 个失败`);
      summary.textContent = parts.join(' · ');
      const prog = $('#gen-progress');
      if (prog) prog.firstElementChild.style.width = `${(100 * done) / s.total}%`;
    }

    const hint = $('#gate-hint');
    // 门禁 B 的说明是分批进度,比 state_reason 更有用,不覆盖
    if (hint && s.state_reason && s.state === lastState && !btnGateB) hint.textContent = s.state_reason;

    if (s.state !== lastState) {
      onStateChanged(s.state);
    } else if (done > lastSucceeded) {
      // 有新镜头出片:不打断用户,给个可点的提示
      const n = done - lastSucceeded;
      lastSucceeded = done;
      toast(`${n} 个${UNIT}已渲染完成`, { type: 'brand', action: { label: '刷新查看', onClick: reloadPreserving } });
    }
  } catch { /* 轮询失败静默重试 */ }
}

function onStateChanged(state) {
  lastState = state;
  if (pendingReload) return;
  pendingReload = true;
  const label = stateMeta(state).label;
  // 正在编辑脚本或看镜头时不强制刷新,免得丢东西
  if (scriptDirty() || lightboxOpen) {
    toast(`项目状态已更新为「${label}」`, { type: 'brand', duration: 0, action: { label: '刷新页面', onClick: reloadPreserving } });
    return;
  }
  toast(`项目状态已更新为「${label}」`, { type: 'brand' });
  setTimeout(reloadPreserving, 1200);
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refreshStatus, POLL_MS);
}
function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}
// 页面切到后台就停,回来立刻补一次
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPolling();
  else { refreshStatus(); startPolling(); }
});

restoreView();
applyShotFilter();
startPolling();
