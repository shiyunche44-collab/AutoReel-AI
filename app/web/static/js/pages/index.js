/* 首页:新建项目表单 + 项目列表筛选 / 搜索 / 删除。 */
import { $, $$, readJSON, withLoading, relTime, debounce, h } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { confirmDialog } from '../ui/modal.js';
import { initChrome } from '../ui/chrome.js';
import { api } from '../api.js';

initChrome();

const DATA = readJSON('page-data', { categories: [], durations: [] });
const form = $('#create-form');

// ---------------------------------------------------------------------------
// 新建表单
// ---------------------------------------------------------------------------
function catInfo(name) {
  return DATA.categories.find((c) => c.name === name) || { themes: [], default_theme: '' };
}

// 方向下拉必须跟着品类走:不同品类的方向词表完全不同(auto 有 7 个,
// beauty 只有一个合成的 overview)。不联动的话,选了美妆再带上 auto
// 的方向名,建项目时会被 400 拒绝。
function syncThemeField() {
  const field = $('#theme-field');
  const sel = $('#theme-select');
  const cat = catInfo($('#category-select').value);
  const themes = cat.themes || [];
  const keep = sel.value;
  sel.replaceChildren(...themes.map((t) => h('option', { value: t.name, title: t.intent || '' }, t.label)));
  // 只有一个隐式默认方向时,单独展示一个下拉框没有意义
  field.classList.toggle('hidden', themes.length <= 1);
  if (keep && themes.some((t) => t.name === keep)) sel.value = keep;
  else if (cat.default_theme && themes.some((t) => t.name === cat.default_theme)) sel.value = cat.default_theme;
  syncThemeIntent();
}

function syncThemeIntent() {
  const sel = $('#theme-select');
  const opt = sel.selectedOptions[0];
  const hint = $('#theme-intent');
  hint.textContent = !$('#theme-field').classList.contains('hidden') && opt && opt.title ? opt.title : '';
}

// 时长档位跟着品类走(auto_long 是 2–8 分钟)
function syncDurations() {
  const group = $('#duration-group');
  const cat = catInfo($('#category-select').value);
  const options = (cat.durations && cat.durations.length) ? cat.durations : DATA.durations;
  const keep = Number(selectedDuration());
  const pick = options.includes(keep) ? keep : options[0];
  const label = (d) => (d >= 120 && d % 60 === 0 ? `${d / 60} 分钟` : `${d} 秒`);
  group.replaceChildren(...options.flatMap((d) => [
    h('input', { type: 'radio', name: 'duration', id: `dur-${d}`, value: String(d), checked: d === pick }),
    h('label', { for: `dur-${d}` }, label(d)),
  ]));
  $$('input[name="duration"]', group).forEach((r) => r.addEventListener('change', refreshEstimate));
}

function selectedDuration() {
  const r = form.querySelector('input[name="duration"]:checked');
  return r ? r.value : DATA.durations[0];
}

let estSeq = 0;
async function refreshEstimate() {
  const seq = ++estSeq;
  const d = selectedDuration();
  const cat = $('#category-select').value;
  $('#est-dur').textContent = `${d} 秒成片`;
  try {
    const data = await api.get(`/api/estimate?duration=${encodeURIComponent(d)}&category=${encodeURIComponent(cat)}`);
    if (seq !== estSeq) return; // 用户连点时只认最后一次
    const wc = data.wall_clock;
    const c = data.cost;
    $('#est-shots').textContent = data.shot_count;
    $('#est-cost').textContent = c.with_retry.toFixed(2);
    // 没有生成排队:实拍铺底 + 叠加层,几分钟就能渲完
    $('#est-time').replaceChildren(`${wc.render_minutes}`, h('small', {}, '分钟'));
    $('#est-time-sub').textContent = `约 ${data.shot_count} 拍 · 横屏 16:9 · 时长跟着旁白走`;
    $('#est-save').textContent = `$${c.tts.toFixed(2)}`;
    $('#est-save-sub').textContent = `BGM $${c.bgm.toFixed(2)} · LLM $${c.llm.toFixed(2)}`;
  } catch {
    if (seq !== estSeq) return;
    for (const id of ['est-shots', 'est-cost', 'est-time', 'est-save']) $('#' + id).textContent = '—';
  }
}

if (form) {
  $('#category-select').addEventListener('change', () => { syncThemeField(); syncDurations(); refreshEstimate(); });
  $('#theme-select').addEventListener('change', syncThemeIntent);
  syncDurations();
  syncThemeField();
  refreshEstimate();

  const subject = $('#subject');
  subject.addEventListener('input', () => {
    subject.classList.remove('is-invalid');
    $('#subject-help').classList.remove('text-danger');
    $('#subject-help').textContent = '尽量写全品牌、车型和年款,事实采集会更准。';
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const subj = String(fd.get('subject') || '').trim();
    if (!subj) {
      subject.classList.add('is-invalid');
      $('#subject-help').classList.add('text-danger');
      $('#subject-help').textContent = '请先填写选题';
      subject.focus();
      return;
    }
    const btn = $('#create-btn');
    await withLoading(btn, async () => {
      try {
        const data = await api.post('/api/projects', {
          subject: subj,
          category: fd.get('category'),
          theme: $('#theme-field').classList.contains('hidden') ? null : (fd.get('theme') || null),
          duration: parseInt(fd.get('duration'), 10),
          quota_budget: fd.get('quota_budget') ? parseFloat(fd.get('quota_budget')) : null,
        });
        toast('项目已创建,正在采集事实…');
        location.href = '/project/' + data.id;
        await new Promise(() => {}); // 跳转期间保持按钮加载态
      } catch (err) {
        toast(err.message, { type: 'danger', title: '创建失败' });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// 项目列表
// ---------------------------------------------------------------------------
const grid = $('#project-grid');
const cards = () => $$('.pcard', grid);
let group = 'all';
let query = '';

function applyFilters() {
  const list = cards();
  const counts = { all: 0, gate: 0, running: 0, done: 0, error: 0 };
  let visible = 0;
  for (const c of list) {
    const g = c.dataset.group;
    const hitQuery = !query || c.dataset.search.includes(query);
    if (hitQuery) {
      counts.all++;
      if (g in counts) counts[g]++;
    }
    const show = hitQuery && (group === 'all' || g === group);
    c.classList.toggle('hidden', !show);
    if (show) visible++;
  }
  for (const [k, v] of Object.entries(counts)) {
    const el = $(`[data-count="${k}"]`);
    if (el) el.textContent = v;
  }
  $('#proj-count').textContent = list.length;
  const empty = $('#proj-empty');
  empty.classList.toggle('hidden', visible > 0);
  if (!visible) {
    const none = list.length === 0;
    $('#proj-empty-title').textContent = none ? '还没有项目' : '没有匹配的项目';
    $('#proj-empty-desc').textContent = none ? '在上面输入一个选题,几分钟后就能看到脚本和分镜。' : '换个关键词或筛选条件试试。';
  }
}

function applySort(key) {
  const attr = { updated: 'updated', created: 'created', cost: 'cost' }[key] || 'updated';
  const sorted = cards().sort((a, b) => Number(b.dataset[attr]) - Number(a.dataset[attr]));
  grid.append(...sorted);
}

if (grid) {
  $$('.rel-time', grid).forEach((el) => { el.textContent = relTime(el.dataset.ts); });

  $$('#proj-filters .chip').forEach((chip) => chip.addEventListener('click', () => {
    group = chip.dataset.group;
    $$('#proj-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
    applyFilters();
  }));

  const search = $('#proj-search');
  search.addEventListener('input', debounce(() => { query = search.value.trim().toLowerCase(); applyFilters(); }, 120));
  search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { search.value = ''; query = ''; applyFilters(); search.blur(); } });

  $('#proj-sort').addEventListener('change', (e) => applySort(e.target.value));

  applyFilters();

  grid.addEventListener('click', async (e) => {
    const btn = e.target.closest('.js-delete');
    if (!btn) return;
    e.preventDefault();
    const ok = await confirmDialog({
      title: '删除这个项目?',
      body: h('div', {},
        h('p', {}, `「${btn.dataset.title}」的事实、脚本、分镜和所有生成的视频文件都会被删除。`),
        h('p', {}, '这个操作无法撤销。')),
      confirmText: '删除项目',
      tone: 'danger',
    });
    if (!ok) return;
    await withLoading(btn, async () => {
      try {
        await api.del(`/api/projects/${btn.dataset.id}`);
        const card = btn.closest('.pcard');
        card.classList.add('is-removing');
        card.addEventListener('animationend', () => { card.remove(); applyFilters(); }, { once: true });
        toast('项目已删除');
      } catch (err) {
        toast(err.message, { type: 'danger', title: '删除失败' });
      }
    });
  });
}

// Ctrl/⌘ + K 聚焦搜索
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    const s = $('#proj-search');
    if (s) { e.preventDefault(); s.focus(); s.select(); }
  }
});
