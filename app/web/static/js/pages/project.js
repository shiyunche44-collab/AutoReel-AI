/* 工作台:Tabs、门禁操作、脚本编辑、渲染看板、状态轮询。 */
import { $, $$, readJSON, withLoading, debounce, h, icon } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { confirmDialog, openModal } from '../ui/modal.js';
import { initChrome, paintBadge, stateMeta } from '../ui/chrome.js';
import { matchShot, matchFact } from '../ui/filters.js';
import { countDirty, isDirty } from '../ui/dirty.js';
import { loadVideo, withFallback } from '../ui/media.js';
import { api } from '../api.js';
import { initConceptStyle } from './concept-style.js';
import { initLookPicker } from './look-pick.js';

initChrome();

// 门禁 A「先补素材」:后台重建素材池(重新找源 + 下载打标签)
const rebuildBtn = $('#rebuild-library');
if (rebuildBtn) {
  rebuildBtn.addEventListener('click', () => withLoading(rebuildBtn, async () => {
    try {
      const r = await api.post(`/api/projects/${DATA.pid}/library/rebuild`);
      toast(r.started ? '素材池已开始后台重建,完成后刷新本页查看' : (r.reason || '没有启动重建'),
        { type: r.started ? 'success' : 'warning' });
    } catch (err) {
      toast(err.message, { type: 'danger' });
    }
  }));
}

const DATA = readJSON('page-data', {});
const PID = DATA.pid;
const RESTORE_KEY = `AutoReel-AI.restore.${PID}`;
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
  try {
    saved = JSON.parse(sessionStorage.getItem(RESTORE_KEY) || 'null');
    sessionStorage.removeItem(RESTORE_KEY);
  } catch {
    /* 忽略 */
  }
  const fromHash = location.hash.slice(1);
  showTab(fromHash || (saved && saved.tab) || content.dataset.defaultTab, { push: !!fromHash });
  if (saved && typeof saved.y === 'number') requestAnimationFrame(() => window.scrollTo(0, saved.y));
}

function reloadPreserving() {
  const active = tabs.find((t) => t.getAttribute('aria-selected') === 'true');
  try {
    sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ tab: active && active.dataset.tab, y: window.scrollY }));
  } catch {
    /* 忽略 */
  }
  location.reload();
}

// ---------------------------------------------------------------------------
// 推进 / 门禁
// ---------------------------------------------------------------------------
/**
 * 触发一次后端动作。返回错误消息(null = 成功);失败已经弹过提示。
 * 返回值给「门禁 A 被 lint 拦下 → 仍然继续」那条分支用。
 */
async function act(btn, url, body, label) {
  return withLoading(btn, async () => {
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
      return null;
    } catch (err) {
      toast(err.message, { type: 'danger', title: `${label}失败` });
      return err.message || '失败';
    }
  });
}

const btnAdvance = $('#btn-advance');
if (btnAdvance)
  btnAdvance.addEventListener('click', () => act(btnAdvance, `/api/projects/${PID}/advance`, undefined, '推进'));

// 概念动画模版面板(本片有概念拍时才显示);门禁 B 上换模版会退回渲染,刷新页面跟上状态
initLookPicker(PID);
const conceptStyle = initConceptStyle(PID, { onRerender: () => setTimeout(reloadPreserving, 700) });

// 门禁 A 被 lint 拦下时,后端错误消息里的标记
const LINT_BLOCK_MARK = '门禁 A 未放行';

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
      body: h(
        'div',
        {},
        h('p', {}, '确认后先合成全片旁白,再按旁白时长逐拍渲染,不调用视频生成。'),
        h('p', {}, `本项目共 ${DATA.shot_count} 个${UNIT},确认后脚本与分镜将不能再修改。`),
        conceptStyle && conceptStyle.hasConcepts()
          ? h('p', {}, `概念动画模版:${conceptStyle.currentName()}(审片时还能换,只重渲概念动画那几拍)`)
          : null,
      ),
      confirmText: '确认并开始渲染',
      tone: 'success',
    });
    if (ok) {
      const err = await act(btnGateA, `/api/projects/${PID}/gate`, { gate: 'A' }, '确认 A');
      // 门禁 A 被 lint 拦下(套话开场、空洞评价、没出处的数字、对比章节拿厂商口径
      // 当论据)。改完再确认最省事,也给一个"明知有问题仍然继续"的出口。
      if (err && err.includes(LINT_BLOCK_MARK)) {
        const go = await confirmDialog({
          title: '脚本还有必须修的问题',
          body: h(
            'div',
            {},
            h(
              'p',
              {},
              '门禁 A 被拦下了:脚本里有机器查得出的问题(套话开场、空洞评价、没出处的数字、对比章节拿厂商口径当论据)。',
            ),
            h('p', {}, '改完再确认最省事 —— 确认时会自动重跑一遍检查。也可以明知有问题直接继续。'),
          ),
          confirmText: '仍然继续',
          cancelText: '我去改',
          tone: 'danger',
        });
        if (go) await act(btnGateA, `/api/projects/${PID}/gate`, { gate: 'A', force: true }, '确认 A');
      }
    }
  });
}

const btnGateB = $('#btn-gate-b');
if (btnGateB) {
  // 草稿就是成片,没有第二遍生成,也就没有分批放行
  $('#gate-hint').textContent =
    `共 ${DATA.total || DATA.shot_count} 拍。不满意的拍可以单独「重拍」换一组背景,全部满意后确认,直接拼接混音出成片。`;
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
const thesisInput = $('#thesis-input');
let originalThesis = thesisInput ? thesisInput.value : '';

function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = `${ta.scrollHeight + 2}px`;
}
function autosizeAll() {
  scriptRows.forEach((r) => autosize(r.querySelector('.script-text')));
}

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
  return isDirty(dirtyFields());
}

/** 当前每一段的文字和当初载入时比,加上论点框。 */
function dirtyFields() {
  const fields = scriptRows.map((r) => ({ now: r.querySelector('.script-text').value, was: original.get(r) }));
  if (thesisInput) fields.push({ now: thesisInput.value, was: originalThesis });
  return fields;
}

function setStatus(text, iconName) {
  const s = $('#save-status');
  if (!s) return;
  s.replaceChildren(...(iconName ? [icon(iconName, 'i-sm')] : []), text);
}

function updateDirty() {
  for (const r of scriptRows) {
    r.classList.toggle('is-dirty', r.querySelector('.script-text').value !== original.get(r));
  }
  if (!savebar || !DATA.editable) return;
  const total = countDirty(dirtyFields());
  savebar.classList.toggle('is-dirty', total > 0);
  setStatus(total ? `${total} 处有未保存的修改` : '所有修改已保存', total ? null : 'check');
}

async function saveScript() {
  if (!saveBtn || saveBtn.disabled) return;
  const segments = scriptRows.map((row) => ({
    idx: parseInt(row.dataset.idx, 10),
    section: row.querySelector('.script-section').textContent.trim(),
    text: row.querySelector('.script-text').value.trim(),
    fact_refs: $$('.script-refs code', row).map((c) => c.textContent.trim()),
  }));
  const thesisInput = $('#thesis-input');
  const thesis = thesisInput ? thesisInput.value.trim() : undefined;
  await withLoading(saveBtn, async () => {
    try {
      const data = await api.patch(`/api/projects/${PID}/script`, { segments, thesis });
      scriptRows.forEach((r) => original.set(r, r.querySelector('.script-text').value));
      if (thesisInput) originalThesis = thesisInput.value;
      updateDirty();
      renderLint(data.issues || []);
      toast(`已保存 ${data.segments} 段脚本`);
    } catch (err) {
      toast(err.message, { type: 'danger', title: '保存失败' });
    }
  });
}

// 门禁 A 的预审报告:机器查得出的问题直接摆出来,人只看剩下的判断题
function renderLint(issues) {
  const panel = $('#lint-panel');
  if (!panel) return;
  const errors = issues.filter((i) => i.severity === 'error');
  const warns = issues.filter((i) => i.severity !== 'error');
  if (!issues.length) {
    panel.hidden = false;
    panel.replaceChildren(
      h(
        'div',
        { class: 'lint-head tone-ok' },
        h('strong', {}, '检查通过'),
        h('span', { class: 'subtle small' }, '没有必须修的问题,可以确认门禁 A'),
      ),
    );
    return;
  }
  panel.hidden = false;
  const line = (i) =>
    h(
      'div',
      { class: 'rowline' },
      h(
        'span',
        { class: `badge tone-${i.severity === 'error' ? 'danger' : 'warning'}` },
        h('span', { class: 'dot' }),
        i.severity === 'error' ? '错误' : '提醒',
      ),
      h('span', { class: 'subtle num' }, `段落 ${i.segment_idx}`),
      h('span', { class: 'grow' }, i.message),
    );
  panel.replaceChildren(
    h(
      'div',
      { class: 'lint-head' },
      h('strong', {}, errors.length ? `${errors.length} 处必须修` : '没有必须修的问题'),
      h('span', { class: 'subtle small' }, warns.length ? `另有 ${warns.length} 条提醒` : ''),
    ),
    h('div', { class: 'rows' }, ...issues.map(line)),
  );
}

if (scriptRows.length) {
  scriptRows.forEach((r) => {
    const ta = r.querySelector('.script-text');
    ta.addEventListener('input', () => {
      autosize(ta);
      updateCounts();
      updateDirty();
    });
  });
  if (thesisInput) thesisInput.addEventListener('input', updateDirty);
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
    if (scriptDirty()) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

// 点击脚本里的事实编号 → 跳到对应事实并高亮
document.addEventListener('click', (e) => {
  const from = /** @type {Element|null} */ (e.target);
  const ref = /** @type {HTMLElement|null} */ (from && from.closest('.js-fact-ref'));
  if (!ref) return;
  const target = document.getElementById(`fact-${ref.dataset.ref}`);
  if (!target) {
    toast(`找不到事实 ${ref.dataset.ref}`, { type: 'warning' });
    return;
  }
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
    const show = matchFact(f.dataset.scope, f.dataset.text, factScope, factQuery);
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
$$('#fact-filters .chip').forEach((chip) =>
  chip.addEventListener('click', () => {
    factScope = chip.dataset.scope;
    $$('#fact-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
    applyFactFilters();
  }),
);
const factSearch = $('#fact-search');
if (factSearch)
  factSearch.addEventListener(
    'input',
    debounce(() => {
      factQuery = factSearch.value.trim().toLowerCase();
      applyFactFilters();
    }, 120),
  );

// ---------------------------------------------------------------------------
// 渲染看板
// ---------------------------------------------------------------------------
const shots = $$('#shot-grid .shot');
let shotFilter = 'all';

function shotMatches(s, f) {
  return matchShot(s.dataset.status, f);
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
$$('#shot-filters .chip').forEach((chip) =>
  chip.addEventListener('click', () => {
    shotFilter = chip.dataset.f;
    $$('#shot-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
    applyShotFilter();
  }),
);
countShots();

// 视频只在进入视口时加载:40 个镜头同时 autoplay 会把浏览器拖垮。
// 能悬停的设备只显示首帧、悬停才播;触屏没有悬停,进入视口就播。
const canHover = matchMedia('(hover: hover)').matches;
let io = null;
function observeVideos() {
  if (io || !shots.length) return;
  const vids = /** @type {HTMLVideoElement[]} */ ($$('#shot-grid video[data-src]'));
  if (canHover) {
    for (const v of vids) {
      const shot = v.closest('.shot');
      const play = () => {
        loadVideo(v);
        shot.classList.add('is-playing');
        v.play().catch(() => {});
      };
      const stop = () => {
        shot.classList.remove('is-playing');
        v.pause();
      };
      shot.addEventListener('mouseenter', play);
      shot.addEventListener('mouseleave', stop);
      shot.addEventListener('focus', play);
      shot.addEventListener('blur', stop);
    }
  }
  if (!('IntersectionObserver' in window)) {
    vids.forEach((v) => {
      loadVideo(v);
      if (!canHover) v.play().catch(() => {});
    });
    return;
  }
  io = new IntersectionObserver(
    (entries) => {
      for (const en of entries) {
        const v = /** @type {HTMLVideoElement} */ (en.target);
        if (en.isIntersecting) {
          loadVideo(v);
          if (!canHover) v.play().catch(() => {});
        } else if (!v.paused) {
          v.pause();
        }
      }
    },
    { rootMargin: '120px' },
  );
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
  const prev = h(
    'button',
    { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '上一个镜头' },
    icon('chevron-left'),
  );
  const next = h(
    'button',
    { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '下一个镜头' },
    icon('chevron-right'),
  );
  const closeBtn = h(
    'button',
    { class: 'btn btn-ghost btn-icon btn-sm', type: 'button', 'aria-label': '关闭' },
    icon('x'),
  );
  const counter = h('span', { class: 'subtle small num' });
  const wrap = h(
    'div',
    { class: 'lightbox' },
    media,
    h(
      'div',
      { class: 'lb-side' },
      h('div', { class: 'modal-head' }, counter, h('div', { class: 'lb-nav' }, prev, next, closeBtn)),
      side,
    ),
  );

  function render() {
    const card = visible[pos];
    const v = card.querySelector('video[data-src]');
    media.replaceChildren(
      v
        ? withFallback(
            h('video', {
              src: v.dataset.src,
              'data-fallback': v.dataset.fallback || '',
              'data-src': v.dataset.src,
              controls: true,
              autoplay: true,
              muted: true,
              loop: true,
              playsinline: true,
            }),
          )
        : card.querySelector('.shot-ph').cloneNode(true),
    );
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
    size: 'modal-lg',
    label: '镜头详情',
    onClose: () => {
      lightboxOpen = false;
      document.removeEventListener('keydown', onKey);
    },
  });
  prev.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  closeBtn.addEventListener('click', () => close());
  document.addEventListener('keydown', onKey);
  el.addEventListener('click', async (e) => {
    const btn = /** @type {Element|null} */ (e.target).closest('.js-retry');
    if (btn) {
      await retryShot(btn);
    }
  });
  render();
}

shots.forEach((s) => {
  s.addEventListener('click', () => openShot(s.dataset.idx));
  s.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openShot(s.dataset.idx);
    }
  });
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
// 一键发布
// ---------------------------------------------------------------------------
/**
 * 打开发布抽屉:选平台(按账号可用性置灰)、看各平台文案、可改标题、立即发。
 *
 * 文案是后端按平台规则生成的(长度、话题、免责声明都在那边统一处理),
 * 这里只做展示和微调 —— 改了会随发布任务一起存下来。
 */
async function openPublish() {
  let preview;
  try {
    preview = await api.get(`/api/projects/${PID}/publish/preview`);
  } catch (err) {
    toast(err.message, { type: 'danger' });
    return;
  }
  const platforms = Object.values(preview.platforms || {});
  if (!platforms.length) {
    toast('还没有平台账号,先去分发台加一个', { type: 'warning' });
    return;
  }
  const published = new Map((preview.published || []).map((p) => [p.platform, p]));

  const picks = new Map();
  const body = h('div', { class: 'pub-drawer' });
  const packaging = packagingPanel(preview.packaging, (title) => {
    // 选了标题:所有平台的标题框都换成它(发布时以框里的为准,仍可逐平台再改)
    for (const pick of picks.values()) pick.titleInput.value = title;
  });
  body.appendChild(packaging.el);
  body.appendChild(
    h(
      'p',
      { class: 'muted small' },
      `这个项目是${preview.aspect_label || preview.aspect},各平台都发这一版成片。`,
    ),
  );
  if (preview.has_output === false) {
    body.appendChild(h('div', { class: 'callout tone-danger' }, '成片还没生成,先出成片。'));
  }

  for (const p of platforms) {
    const done = published.get(p.platform);
    const check = h('input', { type: 'checkbox', class: 'pub-check' });
    check.checked = !done || done.state === 'failed';
    check.disabled = !p.accounts.length;
    const titleInput = h('input', {
      class: 'input',
      type: 'text',
      value: packaging.pickedTitle() || p.title || '',
    });
    const descInput = h('textarea', { class: 'textarea', rows: 3 });
    descInput.value = p.description || '';
    picks.set(p.platform, { check, titleInput, descInput, accountIds: p.accounts.map((a) => a.id) });

    const stateBadge = done
      ? h(
          'span',
          {
            class: `badge tone-${done.state === 'published' ? 'success' : done.state === 'failed' ? 'danger' : 'neutral'}`,
          },
          done.state === 'published' ? '已发布' : done.state === 'failed' ? '上次失败' : done.state,
        )
      : null;

    body.appendChild(
      h(
        'div',
        { class: 'pub-pick-card' },
        h(
          'div',
          { class: 'pub-pick-head' },
          check,
          h('strong', {}, p.rule.label),
          h('span', { class: 'subtle small' }, `推荐 ${p.aspect} · ${p.accounts.length} 个账号`),
          stateBadge,
        ),
        p.aspect_warning ? h('p', { class: 'small tone-warning' }, `${p.aspect_warning},仍可发布。`) : null,
        h(
          'label',
          { class: 'field' },
          h('span', { class: 'field-label' }, `标题(上限 ${p.rule.title_max} 字)`),
          titleInput,
        ),
        h('label', { class: 'field' }, h('span', { class: 'field-label' }, '简介 / 正文'), descInput),
        h('p', { class: 'subtle small' }, `标签:${(p.tags || []).join('、') || '(无)'}`),
      ),
    );
  }

  const content = h(
    'div',
    {},
    h(
      'div',
      { class: 'modal-head' },
      h('div', { class: 'modal-icon tone-brand' }, icon('external', 'i-lg')),
      h(
        'div',
        {},
        h('div', { class: 'modal-title' }, '一键发布'),
        h('div', { class: 'muted small' }, preview.title || ''),
      ),
    ),
    h('div', { class: 'modal-body' }, body),
  );

  const publishBtn = h('button', { class: 'btn btn-primary', type: 'button' }, '发布');
  const cancel = h('button', { class: 'btn btn-ghost', type: 'button' }, '取消');
  content.appendChild(h('div', { class: 'modal-foot' }, cancel, publishBtn));

  const { close } = openModal(content, { size: 'modal-lg', label: '一键发布' });
  cancel.addEventListener('click', () => close());
  publishBtn.addEventListener('click', async () => {
    const accountIds = [];
    const overrides = {};
    for (const [key, pick] of picks) {
      if (!pick.check.checked) continue;
      accountIds.push(...pick.accountIds);
      overrides[key] = {
        title: pick.titleInput.value.trim(),
        description: pick.descInput.value.trim(),
        tags: preview.platforms[key].tags || [],
      };
    }
    if (!accountIds.length) {
      toast('至少勾一个平台', { type: 'warning' });
      return;
    }
    const ok = await confirmDialog({
      title: '发布到这些平台?',
      body: `共 ${accountIds.length} 个账号。发布后不可撤回,各平台会各自走一遍审核。`,
      confirmText: '发布',
    });
    if (!ok) return;
    await withLoading(publishBtn, async () => {
      try {
        const r = await api.post(`/api/projects/${PID}/publish`, { account_ids: accountIds, overrides });
        const n = (r.created || []).length;
        const s = (r.skipped || []).length;
        toast(`已排入 ${n} 条${s ? `,跳过 ${s} 条(已发过)` : ''}`);
        for (const w of r.warnings || []) toast(w, { type: 'warning' });
        close();
      } catch (err) {
        toast(err.message, { type: 'danger' });
      }
    });
  });
}

/**
 * 发布包装:3 个候选标题、2 版封面、置顶评论与备用回复。
 * 选择即时存到后端(发布时按它取封面、记下标题写法,数据回流后能比 A/B);
 * 标题还会同步到各平台的标题框。没有包装(老项目)时给一个「生成」按钮。
 */
function packagingPanel(initial, onTitle) {
  let pkg = initial;
  const el = h('div', { class: 'pkg-card' });
  const kindLabel = { stake: '立场', number: '数字', person: '人群' };

  const copy = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制');
    } catch {
      toast('复制失败,手动选中复制吧', { type: 'warning' });
    }
  };
  const save = async (patch) => {
    try {
      pkg = await api.put(`/api/projects/${PID}/packaging`, patch);
      render();
      return true;
    } catch (err) {
      toast(err.message, { type: 'danger' });
      return false;
    }
  };
  const regenerate = async (btn) => {
    await withLoading(btn, async () => {
      try {
        pkg = await api.post(`/api/projects/${PID}/packaging/regenerate`);
        render();
        onTitle(pickedTitle());
      } catch (err) {
        toast(err.message, { type: 'danger' });
      }
    });
  };
  const pickedTitle = () => (pkg && pkg.titles[pkg.picked.title] ? pkg.titles[pkg.picked.title].text : '');

  function render() {
    el.replaceChildren();
    if (!pkg) {
      const btn = h('button', { class: 'btn btn-sm', type: 'button' }, '生成标题 / 封面文案 / 置顶评论');
      btn.addEventListener('click', () => regenerate(btn));
      el.appendChild(h('div', { class: 'pkg-head' }, h('strong', {}, '发布包装'), btn));
      return;
    }
    const regen = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, '重新生成');
    regen.addEventListener('click', () => regenerate(regen));
    el.appendChild(
      h(
        'div',
        { class: 'pkg-head' },
        h('strong', {}, '发布包装'),
        h('span', { class: 'subtle small' }, pkg.source === 'llm' ? 'AI 生成,数字已对过事实' : '模板兜底'),
        regen,
      ),
    );

    // 标题
    const titles = h('div', { class: 'pkg-titles' });
    pkg.titles.forEach((t, i) => {
      const on = pkg.picked.title === i;
      const row = h(
        'button',
        { type: 'button', class: `pkg-option${on ? ' is-on' : ''}`, 'aria-pressed': String(on) },
        h('span', { class: 'badge tone-neutral' }, kindLabel[t.kind] || t.kind),
        h('span', { class: 'pkg-text' }, t.text),
        t.note ? h('span', { class: 'subtle small' }, t.note) : null,
      );
      row.addEventListener('click', async () => {
        if (await save({ title: i })) onTitle(t.text);
      });
      titles.appendChild(row);
    });
    el.appendChild(titles);

    // 封面
    const covers = h('div', { class: 'pkg-covers' });
    for (const v of ['a', 'b']) {
      if (!pkg.covers[v]) continue;
      const on = pkg.picked.cover === v;
      const btn = h(
        'button',
        { type: 'button', class: `pkg-cover${on ? ' is-on' : ''}`, 'aria-pressed': String(on) },
        h('img', { src: `/api/projects/${PID}/cover?variant=${v}&t=${pkg.made_at || 0}`, alt: `封面 ${v.toUpperCase()}` }),
        h('span', { class: 'small' }, v === 'a' ? '封面 A · 标题版' : '封面 B · 大字版'),
      );
      btn.addEventListener('click', () => save({ cover: v }));
      covers.appendChild(btn);
    }
    if (covers.children.length === 1) {
      covers.appendChild(h('p', { class: 'subtle small' }, '这条片取不到合适的大字,只有一版封面。'));
    }
    el.appendChild(covers);

    // 置顶评论与备用回复:发布后手动贴(平台没有开放评论接口)
    const c = pkg.comment;
    const copyBtn = (text) => {
      const b = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, '复制');
      b.addEventListener('click', () => copy(text));
      return b;
    };
    el.appendChild(
      h(
        'div',
        { class: 'pkg-comment' },
        h('div', { class: 'pkg-line' }, h('span', { class: 'field-label' }, '置顶评论'), copyBtn(c.text)),
        h('p', { class: 'pkg-text' }, c.text),
        pkg.replies.length
          ? h(
              'details',
              {},
              h('summary', { class: 'small subtle' }, `备用回复 ${pkg.replies.length} 条`),
              ...pkg.replies.map((r) => h('div', { class: 'pkg-line' }, h('span', { class: 'small' }, r), copyBtn(r))),
            )
          : null,
      ),
    );
  }
  render();
  return { el, pickedTitle };
}

$('#btn-publish')?.addEventListener('click', openPublish);
$('#btn-publish-out')?.addEventListener('click', openPublish);

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
  } catch {
    /* 轮询失败静默重试 */
  }
}

function onStateChanged(state) {
  lastState = state;
  if (pendingReload) return;
  pendingReload = true;
  const label = stateMeta(state).label;
  // 正在编辑脚本或看镜头时不强制刷新,免得丢东西
  if (scriptDirty() || lightboxOpen) {
    toast(`项目状态已更新为「${label}」`, {
      type: 'brand',
      duration: 0,
      action: { label: '刷新页面', onClick: reloadPreserving },
    });
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
  else {
    refreshStatus();
    startPolling();
  }
});

restoreView();
applyShotFilter();
startPolling();
