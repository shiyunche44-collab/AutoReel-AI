/* 首页:新建项目表单 + 项目列表筛选 / 搜索 / 删除。 */
import { $, $$, readJSON, withLoading, relTime, debounce, h, icon } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { confirmDialog } from '../ui/modal.js';
import { initChrome } from '../ui/chrome.js';
import { matchProject } from '../ui/filters.js';
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

// 方向下拉必须跟着品类走:不同品类的方向词表不同。不联动的话,
// 选了母婴再带上汽车的方向名,建项目时会被 400 拒绝。
// fresh = 刚打开页面 / 换了品类:默认选和最近两条不一样的片型(suggested_theme)
function syncThemeField(fresh = false) {
  const field = $('#theme-field');
  const sel = $('#theme-select');
  const cat = catInfo($('#category-select').value);
  const themes = cat.themes || [];
  const keep = fresh ? cat.suggested_theme : sel.value;
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
  syncRoster();
  syncAngleField();
  syncVariety();
}

// 开头 / 结尾写法:片型各自允许的那几种,第一项「自动」
function syncVariety() {
  const t = currentTheme();
  $('#variety-field').classList.toggle('hidden', !t);
  const fill = (sel, items, auto) => {
    const keep = sel.value;
    sel.replaceChildren(h('option', { value: '' }, auto), ...(items || []).map((x) => h('option', { value: x.id }, x.label)));
    if ((items || []).some((x) => x.id === keep)) sel.value = keep;
  };
  fill($('#opening-select'), t && t.openings, '开头:自动');
  fill($('#ending-select'), t && t.endings, '结尾:自动');
}

// 单点深挖:角度就是"这期只讲的那一个点",必填
const ANGLE_HELP = '填了会额外检索相关近期新闻,开场和标题围绕这个角度来写。';
const ANGLE_PLACEHOLDER = '例:月销破 3 万背后,它到底赢在哪 —— 留空则按内容方向常规讲';
function syncAngleField() {
  const t = currentTheme();
  const need = !!(t && t.requires_angle);
  $('#angle-tag').textContent = need ? '必填' : '可选';
  $('#angle-help').textContent = need ? '这期只讲这一个点(一项技术、一个配置或一个争议参数),别的卖点一概不提。' : ANGLE_HELP;
  $('#angle').placeholder = need ? '例:800V 高压平台到底快在哪' : ANGLE_PLACEHOLDER;
}

// ---------------------------------------------------------------------------
// 多车横评车单:方向是 roundup 时出现,每行一台车 + 可选分组
// ---------------------------------------------------------------------------
function currentTheme() {
  if ($('#theme-field').classList.contains('hidden')) return null;
  const name = $('#theme-select').value;
  return (catInfo($('#category-select').value).themes || []).find((t) => t.name === name) || null;
}

function rosterRange() {
  const t = currentTheme();
  return t && t.subject_count && t.subject_count[1] ? t.subject_count : [3, 5];
}

function rosterRow(name = '', group = '') {
  const row = h(
    'div',
    { class: 'roster-row' },
    h('span', { class: 'roster-no' }),
    h('input', {
      class: 'input roster-name',
      maxlength: '40',
      autocomplete: 'off',
      placeholder: '车型,例:小米YU7',
      value: name,
    }),
    h('input', {
      class: 'input roster-group',
      maxlength: '20',
      autocomplete: 'off',
      placeholder: '分组(可选)',
      value: group,
    }),
    h(
      'button',
      {
        type: 'button',
        class: 'btn btn-ghost btn-sm btn-icon roster-del',
        title: '移除这台车',
        'aria-label': '移除这台车',
      },
      icon('x', 'i-sm'),
    ),
  );
  row.querySelector('.roster-del').addEventListener('click', () => {
    row.remove();
    renumberRoster();
  });
  row.querySelector('.roster-name').addEventListener('input', (e) => e.target.classList.remove('is-invalid'));
  return row;
}

function renumberRoster() {
  const rows = $$('.roster-row', $('#roster'));
  const [lo, hi] = rosterRange();
  rows.forEach((r, i) => {
    r.querySelector('.roster-no').textContent = String(i + 1);
  });
  rows.forEach((r) => {
    r.querySelector('.roster-del').disabled = rows.length <= lo;
  });
  $('#roster-add').disabled = rows.length >= hi;
  $('#roster-count').textContent = `${rows.length} 台 · 需要 ${lo}–${hi} 台`;
}

function syncRoster() {
  const t = currentTheme();
  const on = !!(t && t.roundup);
  $('#roster-field').classList.toggle('hidden', !on);
  const label = $('label[for="subject"]');
  if (on) {
    label.textContent = t.versus ? '对决选题' : '横评选题';
    $('#subject').placeholder = t.versus ? '例:小米YU7 vs 特斯拉Model Y' : '例:25 万级纯电 SUV 横评';
    $('#subject-help').textContent = subjectHelp();
    const roster = $('#roster');
    while ($$('.roster-row', roster).length < rosterRange()[0]) roster.append(rosterRow());
    renumberRoster();
  } else {
    syncSubjectField();
  }
}

function setRoster(names) {
  const roster = $('#roster');
  roster.replaceChildren(...names.map((n) => rosterRow(n)));
  while ($$('.roster-row', roster).length < rosterRange()[0]) roster.append(rosterRow());
  renumberRoster();
}

function rosterValues() {
  return $$('.roster-row', $('#roster'))
    .map((r) => ({
      name: r.querySelector('.roster-name').value.trim(),
      group: r.querySelector('.roster-group').value.trim(),
    }))
    .filter((x) => x.name);
}

// 主体输入框的标签、占位和提示跟着品类走(profile 的 vocab.ui_*)
function subjectHelp() {
  const t = currentTheme();
  if (t && t.roundup) return '这是视频的选题和标题,车型写在下面的车单里。';
  return (catInfo($('#category-select').value).ui || {}).help || '尽量写全品牌和型号,事实采集会更准。';
}

function syncSubjectField() {
  const ui = catInfo($('#category-select').value).ui || {};
  if (ui.subject_label) $('label[for="subject"]').textContent = ui.subject_label;
  if (ui.placeholder) $('#subject').placeholder = ui.placeholder;
  const help = $('#subject-help');
  help.classList.remove('text-danger');
  help.textContent = subjectHelp();
}

// 画幅:建项目时定,之后不能改。默认取品类的画幅
function selectedAspect() {
  const r = form.querySelector('input[name="aspect"]:checked');
  return r ? r.value : '16:9';
}

function syncAspectDefault() {
  const want = catInfo($('#category-select').value).aspect_ratio || '16:9';
  const r = form.querySelector(`input[name="aspect"][value="${want}"]`);
  if (r) r.checked = true;
}

// 时长档位跟着品类和画幅走(横屏 2–8 分钟,竖屏 60 秒–3 分钟)
function syncDurations() {
  const group = $('#duration-group');
  const cat = catInfo($('#category-select').value);
  const byAspect = (cat.durations_by_aspect || {})[selectedAspect()];
  const all =
    byAspect && byAspect.length ? byAspect : cat.durations && cat.durations.length ? cat.durations : DATA.durations;
  // 片型有最长时长(单点深挖最长 4 分钟)
  const theme = currentTheme();
  const cap = theme && theme.max_duration;
  const fit = cap ? all.filter((d) => d <= cap) : all;
  const options = fit.length ? fit : all.slice(0, 1);
  const keep = Number(selectedDuration());
  const pick = options.includes(keep) ? keep : options[0];
  const label = (d) => (d >= 120 && d % 60 === 0 ? `${d / 60} 分钟` : `${d} 秒`);
  group.replaceChildren(
    ...options.flatMap((d) => [
      h('input', { type: 'radio', name: 'duration', id: `dur-${d}`, value: String(d), checked: d === pick }),
      h('label', { for: `dur-${d}` }, label(d)),
    ]),
  );
  $$('input[name="duration"]', group).forEach((r) => r.addEventListener('change', refreshEstimate));
}

// 横评:这个时长最多放得下几台车(竖屏 60 秒只放得下最少台数)
function rosterCapacity() {
  const theme = currentTheme();
  if (!theme || !theme.roundup) return null;
  const cap = (theme.max_subjects || {})[String(selectedDuration())];
  return typeof cap === 'number' ? cap : null;
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
    const data = await api.get(
      `/api/estimate?duration=${encodeURIComponent(d)}&category=${encodeURIComponent(cat)}` +
        `&aspect=${encodeURIComponent(selectedAspect())}`,
    );
    if (seq !== estSeq) return; // 用户连点时只认最后一次
    const wc = data.wall_clock;
    const c = data.cost;
    $('#est-shots').textContent = data.shot_count;
    $('#est-cost').textContent = c.with_retry.toFixed(2);
    // 没有生成排队:实拍铺底 + 叠加层,几分钟就能渲完
    $('#est-time').replaceChildren(`${wc.render_minutes}`, h('small', {}, '分钟'));
    const a = selectedAspect();
    const cap = rosterCapacity();
    $('#est-time-sub').textContent =
      `约 ${data.shot_count} 拍 · ${a === '9:16' ? '竖屏 9:16' : '横屏 16:9'} · 时长跟着旁白走` +
      (cap ? ` · 最多 ${cap} 台车` : '');
    $('#est-save').textContent = `$${c.tts.toFixed(2)}`;
    $('#est-save-sub').textContent = `BGM $${c.bgm.toFixed(2)} · LLM $${c.llm.toFixed(2)}`;
  } catch {
    if (seq !== estSeq) return;
    for (const id of ['est-shots', 'est-cost', 'est-time', 'est-save']) $('#' + id).textContent = '—';
  }
}

if (form) {
  $('#category-select').addEventListener('change', () => {
    syncSubjectField();
    syncThemeField(true);
    syncAspectDefault();
    syncDurations();
    refreshEstimate();
    syncTopicsSource();
  });
  $$('input[name="aspect"]', form).forEach((r) =>
    r.addEventListener('change', () => {
      syncDurations();
      refreshEstimate();
    }),
  );
  $('#theme-select').addEventListener('change', () => {
    syncThemeIntent();
    syncDurations();
    refreshEstimate();
  });
  $('#roster-add').addEventListener('click', () => {
    $('#roster').append(rosterRow());
    renumberRoster();
    $$('.roster-name', $('#roster')).at(-1).focus();
  });
  syncSubjectField();
  syncAspectDefault();
  syncThemeField(true);
  syncDurations();
  refreshEstimate();

  $('#angle').addEventListener('input', (e) => e.target.classList.remove('is-invalid'));
  const subject = $('#subject');
  subject.addEventListener('input', () => {
    subject.classList.remove('is-invalid');
    $('#subject-help').classList.remove('text-danger');
    $('#subject-help').textContent = subjectHelp();
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
    const theme = currentTheme();
    if (theme && theme.requires_angle && !String(fd.get('angle') || '').trim()) {
      $('#angle').classList.add('is-invalid');
      $('#angle').focus();
      toast(`「${theme.label}」要先填选题角度:这期只讲的那一个点`, { type: 'danger', title: '缺少角度' });
      return;
    }
    const roundup = !!(theme && theme.roundup);
    const roster = roundup ? rosterValues() : [];
    if (roundup) {
      const [lo, hi] = rosterRange();
      const unique = new Set(roster.map((x) => x.name.replace(/\s+/g, '').toLowerCase()));
      if (unique.size < lo || unique.size > hi) {
        $$('.roster-name', $('#roster')).forEach((el) => {
          if (!el.value.trim()) el.classList.add('is-invalid');
        });
        toast(`车单需要 ${lo}–${hi} 台不同的车,现在是 ${unique.size} 台`, { type: 'danger', title: '车单不完整' });
        return;
      }
      const cap = rosterCapacity();
      if (cap && unique.size > cap) {
        toast(`${selectedDuration()} 秒最多放得下 ${cap} 台车,请加长时长或减少车数`, {
          type: 'danger',
          title: '时长不够',
        });
        return;
      }
    }
    const btn = $('#create-btn');
    await withLoading(btn, async () => {
      try {
        const data = await api.post('/api/projects', {
          subject: subj,
          category: String(fd.get('category') || ''),
          theme: $('#theme-field').classList.contains('hidden') ? null : fd.get('theme') || null,
          duration: parseInt(String(fd.get('duration')), 10),
          aspect: selectedAspect(),
          angle: String(fd.get('angle') || '').trim() || null,
          opening: theme ? String(fd.get('opening') || '') || null : null,
          ending: theme ? String(fd.get('ending') || '') || null : null,
          ...(roundup ? { subjects: roster } : {}),
          // 选题附带的检索词和热点依据只在车型没被改掉时才有效
          ...(pickedTopic && pickedTopic.subject === subj && String(fd.get('angle') || '').trim()
            ? { angle_search: pickedTopic.search_terms || null, angle_evidence: pickedTopic.evidence || null }
            : {}),
          ...(pickedTopic && pickedTopic.subject === subj && pickedTopic.hook ? { hook: pickedTopic.hook } : {}),
          quota_budget: fd.get('quota_budget') ? parseFloat(String(fd.get('quota_budget'))) : null,
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
// 热点选题:抓热点 → LLM 出单车 / 横评选题 → 点一个填进表单(横评连车单一起填)
// ---------------------------------------------------------------------------
const SOURCE_LABEL = {
  dongchedi: '懂车帝',
  news: '新闻',
  douyin: '抖音',
  bilibili: 'B站',
  llm: '选题生成',
};
const sourceLabel = (s) => SOURCE_LABEL[String(s || '').split('.')[0]] || s;

function renderTopics(data) {
  const grid = $('#topics-grid');
  const topics = data.topics || [];
  grid.replaceChildren(
    ...(topics.length
      ? topics.map((t) => {
          const card = h(
            'button',
            { type: 'button', class: 'topic', 'aria-pressed': 'false' },
            h(
              'div',
              { class: 'topic-head' },
              h('span', { class: 'topic-subject' }, t.subject),
              h('span', { class: 'topic-heat', title: `热度 ${t.heat}/5` }, '🔥'.repeat(t.heat || 1)),
            ),
            (t.subjects || []).length
              ? h(
                  'div',
                  { class: 'topic-roster' },
                  h('span', { class: 'tag tone-brand' }, '横评'),
                  t.subjects.join(' / '),
                )
              : null,
            h('div', { class: 'topic-angle' }, t.angle),
            t.hook ? h('div', { class: 'topic-hook' }, `开场:「${t.hook}」`) : null,
            t.why_hot ? h('div', { class: 'topic-why' }, t.why_hot) : null,
            (t.sources || []).length
              ? h(
                  'div',
                  { class: 'topic-links' },
                  ...t.sources
                    .slice(0, 3)
                    .map((u, i) =>
                      h(
                        'a',
                        { href: u, target: '_blank', rel: 'noopener', onclick: (e) => e.stopPropagation() },
                        `来源 ${i + 1}`,
                      ),
                    ),
                )
              : null,
          );
          card.addEventListener('click', () => pickTopic(t, card));
          return card;
        })
      : [h('div', { class: 'topics-empty' }, '没有生成出选题,可以在下面的原始热点里自己挑。')]),
  );

  const trends = data.trends || [];
  $('#topics-raw-count').textContent = `(${trends.length})`;
  $('#topics-raw-list').replaceChildren(
    ...trends.map((it) =>
      h(
        'div',
        {},
        h('span', { class: 'src' }, `[${sourceLabel(it.source)}]`),
        it.url ? h('a', { href: it.url, target: '_blank', rel: 'noopener' }, it.title) : it.title,
        it.metric ? h('span', { class: 'subtle' }, ` · ${it.metric}`) : null,
      ),
    ),
  );
  $('#topics-raw').open = !topics.length;

  const when = new Date((data.fetched_at || Date.now() / 1000) * 1000);
  $('#topics-meta').textContent =
    `${topics.length} 个选题 · ${trends.length} 条热点 · ${when.toLocaleTimeString().slice(0, 5)} 抓取`;
  $('#topics-body').classList.remove('hidden');
  $('#topics-refresh').classList.remove('hidden');

  const failed = Object.entries(data.errors || {});
  if (failed.length) {
    toast(failed.map(([k, v]) => `${sourceLabel(k)}:${v}`).join(';'), { type: 'warning', title: '部分热点来源不可用' });
  }
}

let pickedTopic = null;

// 热点口径跟着品类走:切品类时换来源说明,上一个品类的选题收起来
function syncTopicsSource() {
  if (!$('#topics-card')) return;
  const ui = catInfo($('#category-select').value).ui || {};
  $('#topics-meta').textContent = ui.trends_label || '新闻 · 抖音热榜 · B站热门';
  $('#topics-body').classList.add('hidden');
  $('#topics-refresh').classList.add('hidden');
  pickedTopic = null;
}

function pickTopic(t, card) {
  pickedTopic = t;
  $$('.topic', $('#topics-grid')).forEach((c) => c.setAttribute('aria-pressed', String(c === card)));
  // 先切方向(横评会换出车单和选题标签),再填内容
  const sel = $('#theme-select');
  if ([...sel.options].some((o) => o.value === t.theme)) {
    sel.value = t.theme;
    syncThemeIntent();
    syncDurations();
    refreshEstimate();
  }
  if ((t.subjects || []).length) setRoster(t.subjects);
  const subject = $('#subject');
  subject.value = t.subject;
  subject.dispatchEvent(new Event('input'));
  $('#angle').value = t.angle;
  form.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadTopics(btn, refresh) {
  await withLoading(btn, async () => {
    try {
      const cat = $('#category-select').value;
      const data = await api.get(`/api/topics?category=${encodeURIComponent(cat)}&refresh=${refresh ? 1 : 0}`);
      renderTopics(data);
    } catch (err) {
      toast(err.message, { type: 'danger', title: '热点抓取失败' });
    }
  });
}

if ($('#topics-card') && form) {
  syncTopicsSource();
  $('#topics-btn').addEventListener('click', (e) => loadTopics(e.currentTarget, false));
  $('#topics-refresh').addEventListener('click', (e) => loadTopics(e.currentTarget, true));
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
    const show = matchProject(g, c.dataset.search, group, query);
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
    $('#proj-empty-desc').textContent = none
      ? '在上面输入一个选题,几分钟后就能看到脚本和分镜。'
      : '换个关键词或筛选条件试试。';
  }
}

function applySort(key) {
  const attr = { updated: 'updated', created: 'created', cost: 'cost' }[key] || 'updated';
  const sorted = cards().sort((a, b) => Number(b.dataset[attr]) - Number(a.dataset[attr]));
  grid.append(...sorted);
}

if (grid) {
  $$('.rel-time', grid).forEach((el) => {
    el.textContent = relTime(el.dataset.ts);
  });

  $$('#proj-filters .chip').forEach((chip) =>
    chip.addEventListener('click', () => {
      group = chip.dataset.group;
      $$('#proj-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
      applyFilters();
    }),
  );

  const search = $('#proj-search');
  search.addEventListener(
    'input',
    debounce(() => {
      query = search.value.trim().toLowerCase();
      applyFilters();
    }, 120),
  );
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      search.value = '';
      query = '';
      applyFilters();
      search.blur();
    }
  });

  $('#proj-sort').addEventListener('change', (e) => applySort(e.target.value));

  applyFilters();

  grid.addEventListener('click', async (e) => {
    const btn = e.target.closest('.js-delete');
    if (!btn) return;
    e.preventDefault();
    const ok = await confirmDialog({
      title: '删除这个项目?',
      body: h(
        'div',
        {},
        h('p', {}, `「${btn.dataset.title}」的事实、脚本、分镜和所有生成的视频文件都会被删除。`),
        h('p', {}, '这个操作无法撤销。'),
      ),
      confirmText: '删除项目',
      tone: 'danger',
    });
    if (!ok) return;
    await withLoading(btn, async () => {
      try {
        await api.del(`/api/projects/${btn.dataset.id}`);
        const card = btn.closest('.pcard');
        card.classList.add('is-removing');
        card.addEventListener(
          'animationend',
          () => {
            card.remove();
            applyFilters();
          },
          { once: true },
        );
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
    if (s) {
      e.preventDefault();
      s.focus();
      s.select();
    }
  }
});
