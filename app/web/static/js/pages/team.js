/* AI 团队:待批选题、成片待审、排期、角色、榜单上升、周报、打法手册、团队动态。每 15 秒刷新一次。 */
import { $, h, icon, relTime, withLoading } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { initChrome } from '../ui/chrome.js';
import { openModal, confirmDialog } from '../ui/modal.js';
import { api } from '../api.js';

initChrome();

const STATUS_TONE = {
  proposed: 'warning', approved: 'brand', producing: 'brand', review: 'warning',
  scheduled: 'brand', published: 'success', rejected: 'neutral', expired: 'neutral', failed: 'danger',
};
const RUN_TONE = { ok: 'success', failed: 'danger', skipped: 'neutral', running: 'brand', idle: 'neutral' };
const RUN_LABEL = { ok: '完成', failed: '失败', skipped: '跳过', running: '运行中', idle: '空闲' };
const WEEK = '日一二三四五六';

let DATA = null;

function when(ts) {
  const d = new Date(ts * 1000);
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return `${d.getMonth() + 1}/${d.getDate()} 周${WEEK[d.getDay()]} ${hm}`;
}

function themeLabel(name) {
  const t = (DATA.themes || []).find((x) => x.name === name);
  return t ? t.label : name;
}

function aspectLabel(a) {
  return a === '9:16' ? '竖屏' : '横屏';
}

function badge(text, tone) {
  return h('span', { class: `badge tone-${tone || 'neutral'}` }, text);
}

function subjectLine(item) {
  const cars = item.subjects && item.subjects.length ? `(${item.subjects.join('、')})` : '';
  return `${item.subject}${cars}`;
}

// ---------------------------------------------------------------------------
// 待批
// ---------------------------------------------------------------------------
function pendingCard(item) {
  const evidence = (item.evidence || []).slice(0, 6);
  const act = (label, cls, fn, ic) =>
    h('button', { class: `btn btn-sm ${cls}`, type: 'button', onclick: (e) => withLoading(e.currentTarget, fn) },
      ic ? icon(ic, 'i-sm') : null, label);
  return h('article', { class: 'plan-card' },
    h('div', { class: 'plan-card-head' },
      h('div', { class: 'grow' },
        h('div', { class: 'plan-subject' }, subjectLine(item)),
        h('div', { class: 'plan-angle' }, item.angle || '(没有角度)')),
      h('div', { class: 'plan-heat', title: '热度 1-5' }, '🔥'.repeat(Math.max(1, Math.min(5, item.heat || 3))))),
    h('div', { class: 'row plan-meta' },
      badge(when(item.slot_at), 'brand'),
      badge(themeLabel(item.theme), 'neutral'),
      badge(`${aspectLabel(item.aspect)} ${item.duration}s`, 'neutral')),
    item.rationale ? h('p', { class: 'muted small' }, item.rationale) : null,
    item.hook ? h('p', { class: 'small' }, '开场:', h('strong', {}, item.hook)) : null,
    evidence.length
      ? h('details', { class: 'small' }, h('summary', { class: 'subtle' }, `热点依据 ${evidence.length} 条`),
          h('ul', { class: 'plan-evidence' }, evidence.map((e) => h('li', {}, e))))
      : null,
    h('div', { class: 'row plan-actions' },
      act('批准', 'btn-primary', () => call(`/api/team/plan/${item.id}/approve`, '已批准'), 'check'),
      act('改', 'btn-ghost', () => editItem(item), 'pen'),
      act('驳回', 'btn-ghost', () => rejectItem(item), 'x'),
      h('span', { class: 'grow' }),
      act('马上做', 'btn-ghost', () => startItem(item), 'play')),
  );
}

async function call(url, ok, body) {
  try {
    await api.post(url, body);
    if (ok) toast(ok);
    await load();
  } catch (err) {
    toast(err.message, { type: 'danger' });
  }
}

async function rejectItem(item) {
  const yes = await confirmDialog({
    title: '驳回这条选题?', body: `「${subjectLine(item)}」。空档会在主编下一轮补上。`,
    confirmText: '驳回', tone: 'danger',
  });
  if (yes) await call(`/api/team/plan/${item.id}/reject`, '已驳回', { note: '' });
}

async function startItem(item) {
  const yes = await confirmDialog({
    title: '马上开工?', body: '不等排期,现在就建项目、抓事实、写脚本(会消耗额度)。',
    confirmText: '开工',
  });
  if (!yes) return;
  try {
    const res = await api.post(`/api/team/plan/${item.id}/start`);
    toast('已开工', { action: { label: '打开项目', onClick: () => (location.href = `/project/${res.project_id}`) } });
    await load();
  } catch (err) {
    toast(err.message, { type: 'danger', title: '开工失败' });
  }
}

function editItem(item) {
  const themes = DATA.themes || [];
  const field = (label, control) => h('label', { class: 'field' }, h('span', { class: 'field-label' }, label), control);
  const subject = h('input', { class: 'input', value: item.subject });
  const angle = h('input', { class: 'input', value: item.angle || '', maxlength: '120' });
  const hook = h('input', { class: 'input', value: item.hook || '', maxlength: '40' });
  const theme = h('select', { class: 'select' },
    themes.map((t) => h('option', { value: t.name, selected: t.name === item.theme }, t.label)));
  const aspect = h('select', { class: 'select' },
    ['16:9', '9:16'].map((a) => h('option', { value: a, selected: a === item.aspect }, aspectLabel(a))));
  const duration = h('select', { class: 'select' });
  const fillDurations = () => {
    const t = themes.find((x) => x.name === theme.value);
    const opts = (t && t.durations[aspect.value]) || [];
    const keep = Number(duration.value || item.duration);
    duration.replaceChildren(...opts.map((d) => h('option', { value: d, selected: d === keep }, `${d}s`)));
  };
  theme.addEventListener('change', fillDurations);
  aspect.addEventListener('change', fillDurations);
  fillDurations();

  const save = h('button', { class: 'btn btn-primary', type: 'button' }, '保存');
  const cancel = h('button', { class: 'btn btn-ghost', type: 'button' }, '取消');
  const content = h('div', {},
    h('div', { class: 'modal-head' }, h('div', { class: 'modal-title' }, '改选题')),
    h('div', { class: 'modal-body stack-sm' },
      field('车型 / 选题名', subject), field('角度', angle), field('开场钩子', hook),
      h('div', { class: 'form-row' }, field('内容方向', theme), field('画幅', aspect), field('时长', duration))),
    h('div', { class: 'modal-foot' }, cancel, save));
  const { close } = openModal(content, { label: '改选题' });
  cancel.addEventListener('click', () => close(false));
  save.addEventListener('click', () => withLoading(save, async () => {
    try {
      await api.patch(`/api/team/plan/${item.id}`, {
        subject: subject.value.trim(), angle: angle.value.trim(), hook: hook.value.trim(),
        theme: theme.value, aspect: aspect.value, duration: Number(duration.value),
      });
      close(true);
      toast('已保存');
      await load();
    } catch (err) {
      toast(err.message, { type: 'danger' });
    }
  }));
}

// ---------------------------------------------------------------------------
// 成片待审
// ---------------------------------------------------------------------------
function localInput(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

const CHECK_TONE = { ok: 'success', warn: 'warning', red: 'danger' };
const CHECK_ICON = { ok: 'check', warn: 'alert-circle', red: 'x' };

function checklistView(sheet) {
  if (!sheet) return null;
  const head = sheet.red
    ? `合规审核单 · ${sheet.red} 项红`
    : sheet.warn ? `合规审核单 · ${sheet.warn} 项提醒` : '合规审核单 · 全部通过';
  return h('div', { class: 'checklist' },
    h('div', { class: `checklist-head text-${sheet.red ? 'danger' : sheet.warn ? 'warning' : 'success'}` },
      icon('shield', 'i-sm'), head),
    ...sheet.items.map((c) => h('div', { class: `check-row is-${c.level}` },
      h('span', { class: `check-mark text-${CHECK_TONE[c.level]}` }, icon(CHECK_ICON[c.level], 'i-sm')),
      h('span', { class: 'check-label' }, c.label),
      h('span', { class: 'subtle small check-detail' }, c.detail || ''))));
}

function healthBanner(row) {
  const btn = h('button', { class: 'btn btn-sm', type: 'button' }, icon('play', 'i-sm'), '恢复自动发布');
  btn.addEventListener('click', async () => {
    const yes = await confirmDialog({
      title: `恢复「${row.name}」自动发布?`,
      body: '先去抖音创作者中心确认作品状态正常(没被限流、没下架)。恢复后排着的发布会按时发出。',
      confirmText: '恢复',
    });
    if (yes) await call(`/api/team/accounts/${row.account_id}/resume`, '已恢复自动发布');
  });
  return h('div', { class: 'callout tone-danger health-banner' }, icon('alert-circle'),
    h('div', { class: 'grow' }, h('strong', {}, `「${row.name}」已暂停自动发布`), ` · ${row.reason}`,
      h('div', { class: 'subtle small' }, `合规审核官 ${relTime(row.paused_at)}判定`)),
    btn);
}

function reviewCard(item) {
  const rv = item.review || {};
  const accounts = DATA.accounts || [];
  const title = h('input', { class: 'input', value: rv.title || '', maxlength: '60', list: `titles-${item.id}` });
  const titles = h('datalist', { id: `titles-${item.id}` }, (rv.titles || []).map((t) => h('option', { value: t })));
  const at = h('input', { class: 'input', type: 'datetime-local', value: localInput(rv.publish_at || item.slot_at) });
  const boxes = accounts.map((a) => {
    const box = h('input', { type: 'checkbox', value: a.id, checked: a.default, disabled: a.paused });
    return {
      box,
      el: h('label', { class: `chip-check${a.paused ? ' is-paused' : ''}`, title: a.paused_reason || '' },
        box, `${a.platform_label} · ${a.name}`, a.paused ? '(已暂停)' : ''),
    };
  });
  const red = rv.checklist?.red || 0;
  const pass = h('button', {
    class: 'btn btn-sm btn-primary', type: 'button', disabled: red > 0,
    title: red ? '审核单有红项:去项目页改好再来' : '',
  }, icon('check', 'i-sm'), '通过,定时发布');
  pass.addEventListener('click', () => withLoading(pass, async () => {
    const ids = boxes.filter((b) => b.box.checked).map((b) => b.box.value);
    if (!ids.length) {
      toast('至少选一个发布账号', { type: 'danger' });
      return;
    }
    try {
      const res = await api.post(`/api/team/plan/${item.id}/publish`, {
        account_ids: ids, title: title.value.trim(), publish_at: new Date(at.value).getTime() / 1000,
      });
      toast(`已排 ${when(res.publish_at)} 发布(${res.created} 个账号)`);
      (res.warnings || []).forEach((w) => toast(w, { type: 'warning' }));
      await load();
    } catch (err) {
      toast(err.message, { type: 'danger', title: '没排上' });
    }
  }));
  const back = h('button', { class: 'btn btn-sm btn-ghost', type: 'button' }, icon('x', 'i-sm'), '打回');
  back.addEventListener('click', async () => {
    const yes = await confirmDialog({
      title: '打回这条成片?', body: '这条不发了。要重做就去项目页改,或者让主编另排。', confirmText: '打回', tone: 'danger',
    });
    if (yes) await call(`/api/team/plan/${item.id}/reject-cut`, '已打回', { note: '' });
  });
  const portrait = item.aspect === '9:16';
  return h('article', { class: `plan-card review-card${portrait ? ' is-portrait' : ''}` },
    h('div', { class: 'review-media' },
      rv.has_video
        ? h('video', { src: rv.video_url, poster: rv.cover_url, controls: true, preload: 'none' })
        : empty('成片文件不在了')),
    h('div', { class: 'review-form stack-sm' },
      h('div', {},
        h('div', { class: 'plan-subject' }, subjectLine(item)),
        h('div', { class: 'plan-angle' }, item.angle || rv.project_title || '')),
      checklistView(rv.checklist),
      h('label', { class: 'field' }, h('span', { class: 'field-label' }, '发布标题'), title, titles),
      h('label', { class: 'field' }, h('span', { class: 'field-label' }, '发布时间'), at),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, '发到'),
        accounts.length
          ? h('div', { class: 'row wrap-row' }, boxes.map((b) => b.el))
          : h('span', { class: 'subtle small' }, '还没有可用账号,先去', h('a', { href: '/publish' }, '分发台'), '添加')),
      h('div', { class: 'row plan-actions' }, pass, back, h('span', { class: 'grow' }),
        h('a', { class: 'btn btn-sm btn-ghost', href: `/project/${item.project_id}` }, icon('external', 'i-sm'), '项目'))),
  );
}

// ---------------------------------------------------------------------------
// 排期 / 角色 / 上升 / 动态
// ---------------------------------------------------------------------------
function planRow(item) {
  const link = item.project_id
    ? h('a', { class: 'btn btn-sm btn-ghost', href: `/project/${item.project_id}` }, icon('external', 'i-sm'), '项目')
    : null;
  const held = item.status === 'producing' && item.hold;
  const retry = held
    ? h('button', {
      class: 'btn btn-sm btn-ghost', type: 'button', title: '在项目页改好后点这里,制片下一轮重新过闸',
      onclick: (e) => withLoading(e.currentTarget, () => call(`/api/team/plan/${item.id}/retry-gate`, '下一轮重新过闸')),
    }, icon('refresh', 'i-sm'), '重试')
    : null;
  const tail = item.status === 'scheduled' && item.publish_at ? `发布 ${when(item.publish_at)}` : '';
  return h('div', { class: 'plan-row' },
    h('div', { class: 'rowline' },
      h('span', { class: 'num subtle plan-when' }, when(item.slot_at)),
      h('span', { class: 'grow' }, h('strong', {}, subjectLine(item)), ' ',
        h('span', { class: 'subtle small' }, item.angle || '')),
      h('span', { class: 'subtle small' }, tail || `${themeLabel(item.theme)} · ${aspectLabel(item.aspect)} ${item.duration}s`),
      item.note ? h('span', { class: 'subtle small', title: item.note }, icon('info', 'i-sm')) : null,
      held ? badge('等你处理', 'warning') : badge(item.status_label, STATUS_TONE[item.status]),
      retry, link),
    held ? h('div', { class: 'plan-hold small' }, item.hold) : null);
}

function roleRow(r) {
  const last = r.last_work || r.last;
  const runBtn = h('button', { class: 'btn btn-sm btn-ghost', type: 'button', 'data-run': r.name },
    icon('play', 'i-sm'), '立即运行');
  return h('div', { class: 'role-row' },
    h('div', { class: 'row' },
      h('strong', {}, r.label), h('span', { class: 'subtle small' }, r.cadence),
      h('span', { class: 'grow' }),
      last ? badge(RUN_LABEL[last.status] || last.status, RUN_TONE[last.status]) : badge('还没跑过', 'neutral'),
      runBtn),
    h('div', { class: 'muted small' }, r.duty),
    last && last.summary ? h('div', { class: 'small' }, `${relTime(last.started_at)}:${last.summary}`) : null,
    h('div', { class: 'subtle small' }, `下次:${DATA.paused || !DATA.enabled ? '已暂停' : when(r.next_at)}`));
}

function risingRow(m) {
  const src = m.source === 'dongchedi.sales' ? '销量榜' : '热度榜';
  const move = m.delta == null ? badge('新上榜', 'warning') : badge(`↑${m.delta}`, 'success');
  return h('div', { class: 'rowline' },
    h('span', { class: 'grow' }, m.car), h('span', { class: 'subtle small' }, `${src} 第 ${m.rank}`), move);
}

function runRow(x) {
  const r = (DATA.roles || []).find((y) => y.name === x.role);
  return h('div', { class: 'rowline' },
    h('span', { class: 'subtle small num' }, relTime(x.started_at)),
    h('strong', {}, r ? r.label : x.role),
    x.trigger === 'manual' ? badge('手动', 'neutral') : null,
    h('span', { class: 'grow small' }, x.summary || ''),
    badge(RUN_LABEL[x.status] || x.status, RUN_TONE[x.status]));
}

// ---------------------------------------------------------------------------
// 复盘官:周报 + 打法手册
// ---------------------------------------------------------------------------
function pct(x) {
  return x == null ? '—' : `${(x * 100).toFixed(1)}%`;
}

function workLine(w) {
  return h('div', { class: 'report-work' },
    h('a', { href: `/project/${w.project_id}` }, w.title || w.project_id),
    h('div', { class: 'row' },
      h('span', { class: 'subtle small num grow' }, `${w.views ?? '—'} 播放 · 完播 ${pct(w.completion_rate)}`),
      w.lift != null ? badge(`${w.lift}×`, w.lift >= 1 ? 'success' : 'neutral') : null));
}

function reportView(r) {
  if (!r) return [empty('复盘官每周日 20:00 出周报(要有发布满 24 小时的作品)')];
  const t = r.totals;
  const out = [
    h('div', { class: 'report-stats' },
      ...[['本周新发', t.week_works], ['24h 播放', t.week_views], ['涨粉', t.week_followers],
        ['播放中位数', t.median_views], ['平均完播', pct(t.completion_rate)]]
        .map(([k, v]) => h('div', {}, h('div', { class: 'subtle small' }, k), h('strong', { class: 'num' }, String(v ?? '—'))))),
  ];
  if (r.summary) out.push(h('p', { class: 'report-text' }, r.summary));
  if (r.next && r.next.length) {
    out.push(h('div', { class: 'subtle small' }, '下周怎么做'),
      h('ul', { class: 'report-next' }, ...r.next.map((x) => h('li', {}, x))));
  }
  if (r.top && r.top.length) out.push(h('div', { class: 'subtle small' }, '最好'), ...r.top.map(workLine));
  if (r.bottom && r.bottom.length) out.push(h('div', { class: 'subtle small' }, '最差'), ...r.bottom.map(workLine));
  if (r.experiments && r.experiments.length) {
    out.push(h('div', { class: 'subtle small' }, '下周实验'),
      h('div', { class: 'row wrap-row' }, ...r.experiments.map((e) => badge(`${e.label} · ${e.why}`, 'brand'))));
  }
  return out;
}

function learningRow(l) {
  const off = l.status === 'off';
  const btn = h('button', {
    class: 'btn btn-sm btn-ghost', type: 'button',
    title: off ? '恢复后重新回灌' : '觉得这条是巧合(比如那周是热点带的)就停用,停用的不回灌',
    onclick: (e) => withLoading(e.currentTarget, () => call(
      `/api/team/learnings/${encodeURIComponent(l.id)}/status`, off ? '已恢复' : '已停用',
      { status: off ? 'active' : 'off' })),
  }, icon(off ? 'play' : 'pause', 'i-sm'), off ? '恢复' : '停用');
  const ev = (l.evidence || []).map((x) => `《${x.title}》${x.lift}×`).join('\n');
  return h('div', { class: `rowline${off ? ' is-off' : ''}` },
    badge(l.effect >= 1 ? '多做' : '少做', l.effect >= 1 ? 'success' : 'warning'),
    h('span', { class: 'grow small', title: ev }, l.text),
    off ? badge('已停用', 'neutral') : null,
    btn);
}

function empty(text) {
  return h('div', { class: 'empty' }, h('div', { class: 'empty-title' }, text));
}

function render() {
  const pending = DATA.plan.filter((i) => i.status === 'proposed');
  const rest = DATA.plan.filter((i) => i.status !== 'proposed');
  $('#pending-count').textContent = String(pending.length);
  $('#btn-approve-all').disabled = !pending.length;
  $('#pending-list').replaceChildren(
    ...(pending.length ? pending.map(pendingCard) : [empty('没有待批的选题。主编每天 09:00 补空档。')]));
  $('#health-list').replaceChildren(...(DATA.health || []).map(healthBanner));
  const reviews = rest.filter((i) => i.status === 'review');
  $('#review-count').textContent = String(reviews.length);
  $('#review-list').replaceChildren(
    ...(reviews.length ? reviews.map(reviewCard) : [empty('没有待审的成片')]));
  $('#plan-list').replaceChildren(...(rest.length ? rest.map(planRow) : [empty('还没有排上的选题')]));
  const s = DATA.settings;
  $('#plan-hint').textContent =
    `每天 ${s.daily_slots} 条,发布 ${s.slot_times.join('、')},提前 ${s.lead_hours} 小时开工,同车 ${s.cooldown_days} 天冷却`;
  $('#role-list').replaceChildren(...DATA.roles.map(roleRow));
  $('#rising-list').replaceChildren(
    ...(DATA.rising.length ? DATA.rising.map(risingRow) : [empty('需要至少两批热点才能比名次')]));
  $('#trend-at').textContent = DATA.trends_at ? `最近一批 ${relTime(DATA.trends_at)} · ${DATA.trends_n} 条` : '';
  $('#run-list').replaceChildren(...(DATA.runs.length ? DATA.runs.map(runRow) : [empty('还没有动态')]));
  $('#report-body').replaceChildren(...reportView(DATA.report));
  $('#report-at').textContent = DATA.report
    ? `${DATA.report.week} · ${relTime(DATA.report.created_at)}${DATA.report.source === 'template' ? ' · 模板' : ''}` : '';
  const book = DATA.playbook || [];
  $('#playbook-list').replaceChildren(
    ...(book.length ? book.map(learningRow) : [empty('还没有结论:同一种做法至少 3 条作品、效果偏离 15% 以上才会写进来')]));

  const state = $('#team-state');
  const off = !DATA.enabled;
  state.textContent = off ? '已关闭(TEAM_ENABLED=0)' : DATA.paused ? '已暂停' : '运行中';
  state.className = `badge tone-${off || DATA.paused ? 'neutral' : 'success'}`;
  const pauseBtn = $('#btn-pause');
  pauseBtn.querySelector('span').textContent = DATA.paused ? '恢复团队' : '暂停团队';
  pauseBtn.disabled = off;
}

async function load() {
  try {
    DATA = await api.get('/api/team');
    render();
  } catch (err) {
    toast(err.message, { type: 'danger', title: '加载失败' });
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-run]');
  if (!btn) return;
  withLoading(btn, async () => {
    await call(`/api/team/roles/${btn.dataset.run}/run`, '已让它开始干活,稍后看动态');
  });
});

$('#btn-approve-all').addEventListener('click', (e) =>
  withLoading(e.currentTarget, async () => {
    const res = await api.post('/api/team/plan/approve-all').catch((err) => toast(err.message, { type: 'danger' }));
    if (res) toast(`批准了 ${res.approved} 条`);
    await load();
  }));

$('#btn-pause').addEventListener('click', (e) =>
  withLoading(e.currentTarget, async () => {
    await call('/api/team/pause', DATA.paused ? '团队已恢复' : '团队已暂停', { paused: !DATA.paused });
  }));

load();
setInterval(() => {
  // 正在填成片审核表单 / 看片时别刷新,不然输入和播放进度都会丢
  const busy = document.querySelector('.modal-backdrop')
    || document.activeElement?.closest?.('.review-card')
    || [...document.querySelectorAll('.review-card video')].some((v) => !v.paused);
  if (!document.hidden && !busy) load();
}, 15000);
