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
/** 打法手册的预测倍数:车型、片型、时长、画幅各自的权重取几何平均。没结论 = 1,不显示。 */
function predictBadge(p) {
  if (p == null || Math.abs(p - 1) < 0.05) return null;
  const el = badge(`预测 ×${p.toFixed(2)}`, p > 1 ? 'success' : 'warning');
  el.title = '按打法手册:这类选题过去的效果相对账号平常水平的倍数';
  return el;
}

function pendingCard(item) {
  const evidence = (item.evidence || []).slice(0, 6);
  const act = (label, cls, fn, ic) =>
    h('button', { class: `btn btn-sm ${cls}`, type: 'button', onclick: (e) => withLoading(e.currentTarget, fn) },
      ic ? icon(ic, 'i-sm') : null, label);
  const rapid = item.kind === 'rapid';
  return h('article', { class: `plan-card${rapid ? ' is-rapid' : ''}` },
    h('div', { class: 'plan-card-head' },
      h('div', { class: 'grow' },
        h('div', { class: 'plan-subject' },
          rapid ? h('span', { class: 'badge tone-danger rapid-badge' }, icon('zap', 'i-sm'), '快反') : null,
          subjectLine(item)),
        h('div', { class: 'plan-angle' }, item.angle || '(没有角度)')),
      h('div', { class: 'plan-heat', title: '热度 1-5' }, '🔥'.repeat(Math.max(1, Math.min(5, item.heat || 3))))),
    h('div', { class: 'row plan-meta' },
      badge(when(item.slot_at), 'brand'),
      badge(themeLabel(item.theme), 'neutral'),
      badge(`${aspectLabel(item.aspect)} ${item.duration}s`, 'neutral'),
      predictBadge(item.predict)),
    item.rationale ? h('p', { class: 'muted small' }, item.rationale) : null,
    rapid && item.note ? h('p', { class: 'small text-danger' }, icon('alert-triangle', 'i-sm'), ' ', item.note) : null,
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

const TOPIC_REASONS = ['车型不想做', '角度没意思', '最近做过类似的', '热度不够', '不适合本号'];
const CUT_REASONS = ['开场没抓人', '节奏拖', '画面 / 素材不行', '配音别扭', '标题不对味', '事实有问题'];

/** 驳回 / 打回:选个理由或自己写一句(可空)。理由会被复盘官归纳成「你的偏好」。取消 → null。 */
function reasonDialog({ title, body, confirmText, reasons }) {
  return new Promise((resolve) => {
    const note = h('input', { class: 'input', maxlength: '120', placeholder: '为什么?(可不填)' });
    const chips = h('div', { class: 'chips' }, reasons.map((r) => h('button', {
      class: 'chip', type: 'button', 'aria-pressed': 'false',
      onclick: (e) => {
        const on = e.currentTarget.getAttribute('aria-pressed') !== 'true';
        e.currentTarget.setAttribute('aria-pressed', String(on));
        const picked = [...chips.querySelectorAll('[aria-pressed="true"]')].map((c) => c.textContent);
        note.value = picked.join(';');
      },
    }, r)));
    const ok = h('button', { class: 'btn btn-danger', type: 'button' }, confirmText);
    const cancel = h('button', { class: 'btn btn-ghost', type: 'button' }, '取消');
    const content = h('div', {},
      h('div', { class: 'modal-head' }, h('div', { class: 'modal-title' }, title)),
      h('div', { class: 'modal-body stack-sm' },
        h('p', { class: 'small' }, body), chips, note,
        h('div', { class: 'subtle small' }, '理由会被复盘官归纳成「你的偏好」,以后排选题、写脚本照着来')),
      h('div', { class: 'modal-foot' }, cancel, ok));
    const { close } = openModal(content, { label: title, onClose: (r) => resolve(r || null) });
    cancel.addEventListener('click', () => close(null));
    ok.addEventListener('click', () => close({ note: note.value.trim() }));
    note.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
  });
}

async function rejectItem(item) {
  const res = await reasonDialog({
    title: '驳回这条选题?', body: `「${subjectLine(item)}」。空档会在主编下一轮补上。`,
    confirmText: '驳回', reasons: TOPIC_REASONS,
  });
  if (res) await call(`/api/team/plan/${item.id}/reject`, '已驳回', res);
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

// 分发官的衍生品:图文笔记 + 竖版切片,各自勾发到哪些账号
function pieceView(p) {
  const red = (p.problems || []).length > 0;
  const boxes = (p.accounts || []).map((a) => {
    const box = h('input', { type: 'checkbox', value: a.id, checked: a.default, disabled: a.paused || red });
    return { box, el: h('label', { class: 'chip-check' }, box, `${a.platform_label} · ${a.name}`) };
  });
  const media = p.kind === 'note'
    ? h('div', { class: 'piece-strip' }, p.images.map((src) => h('a', { href: src, target: '_blank' },
      h('img', { src, loading: 'lazy', alt: '' }))))
    : h('video', { class: 'piece-clip', src: p.url, controls: true, preload: 'none' });
  const text = p.kind === 'note'
    ? h('details', { class: 'piece-body' }, h('summary', {}, p.title),
      h('pre', {}, p.body), p.tags?.length ? h('div', { class: 'subtle small' }, p.tags.map((t) => `#${t}`).join(' ')) : null)
    : h('div', {}, h('div', {}, p.title), h('div', { class: 'subtle small' }, p.why));
  const el = h('div', { class: `piece${red ? ' is-red' : ''}` },
    h('div', { class: 'piece-head' }, h('strong', {}, p.label),
      red ? h('span', { class: 'text-danger small' }, icon('x', 'i-sm'), p.problems.join(';')) : null),
    media, text,
    boxes.length
      ? h('div', { class: 'row wrap-row' }, boxes.map((b) => b.el))
      : h('span', { class: 'subtle small' }, p.kind === 'note' ? '没有小红书账号,先去分发台添加' : '没有竖屏平台账号'));
  return { el, ids: () => boxes.filter((b) => b.box.checked).map((b) => b.box.value) };
}

function piecesView(info) {
  if (!info) return { el: null, picks: () => ({}) };
  if (!info.ready) {
    return { el: h('div', { class: 'subtle small' }, icon('clock', 'i-sm'), '分发官还在出图文笔记和切片,出好了这里会显示'), picks: () => ({}) };
  }
  const views = info.pieces.map((p) => ({ id: p.id, ...pieceView(p) }));
  const el = h('div', { class: 'field pieces' },
    h('span', { class: 'field-label' }, '一稿多发(和主片一起通过)'),
    ...views.map((v) => v.el),
    ...(info.errors || []).map((e) => h('div', { class: 'text-warning small' }, e)),
    views.length ? null : h('span', { class: 'subtle small' }, '这条没出成衍生品'));
  const picks = () => Object.fromEntries(views.map((v) => [v.id, v.ids()]).filter(([, ids]) => ids.length));
  return { el, picks };
}

function reviewCard(item) {
  const rv = item.review || {};
  const pieces = piecesView(rv.pieces);
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
        pieces: pieces.picks(),
      });
      const extra = res.pieces?.length ? `;另排 ${res.pieces.join('、')}` : '';
      toast(`已排 ${when(res.publish_at)} 发布(${res.created} 个账号)${extra}`);
      (res.warnings || []).forEach((w) => toast(w, { type: 'warning' }));
      await load();
    } catch (err) {
      toast(err.message, { type: 'danger', title: '没排上' });
    }
  }));
  const back = h('button', { class: 'btn btn-sm btn-ghost', type: 'button' }, icon('x', 'i-sm'), '打回');
  back.addEventListener('click', async () => {
    const res = await reasonDialog({
      title: '打回这条成片?', body: '这条不发了。要重做就去项目页改,或者让主编另排。',
      confirmText: '打回', reasons: CUT_REASONS,
    });
    if (res) await call(`/api/team/plan/${item.id}/reject-cut`, '已打回', res);
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
      pieces.el,
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
      h('span', { class: 'grow' }, item.kind === 'rapid' ? badge('快反', 'danger') : null, ' ',
        h('strong', {}, subjectLine(item)), ' ',
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
  if (r.early && r.early.length) {
    out.push(h('div', { class: 'subtle small' }, '早期观察(发布 1 小时,还没定稿)'));
    out.push(h('div', { class: 'early-list' }, ...r.early.map((e) => h('div', { class: `early-row is-${e.level}` },
      h('div', { class: 'row' },
        h('strong', { class: 'grow' }, e.title || '—'),
        badge(e.level === 'bad' ? '前 2 秒掉人' : e.level === 'warn' ? '留不住' : '正常',
          e.level === 'bad' ? 'danger' : e.level === 'warn' ? 'warning' : 'success')),
      h('div', { class: 'subtle small num' },
        `发布 ${e.hours ?? '—'} 小时 · ${e.views ?? '—'} 播放 · 2 秒留存 ${pct(e.retention_2s)}`
        + ` · 平均观看 ${e.avg_watch_sec != null ? `${Number(e.avg_watch_sec).toFixed(1)} 秒` : '—'}`),
      ...(e.advice || []).map((a) => h('div', { class: 'small' }, `要改:${a}`))))));
  }
  if (r.experiments && r.experiments.length) {
    out.push(h('div', { class: 'subtle small' }, '下周实验'),
      h('div', { class: 'row wrap-row' }, ...r.experiments.map((e) => badge(`${e.label} · ${e.why}`, 'brand'))));
  }
  const b = r.benchmark;
  if (b && b.theirs && b.theirs.length) {
    const col = (title, rows) => h('div', { class: 'bench-col' },
      h('div', { class: 'subtle small' }, title),
      ...rows.map((x) => h('div', { class: 'small bench-open', title: x.title }, h('strong', {}, x.opening || '—'), x.tag)));
    out.push(h('div', { class: 'subtle small' }, '标题开头:对标爆款 vs 我们最好的'
      + (b.their_avg_duration ? `(对标爆款平均 ${Math.round(b.their_avg_duration)} 秒)` : '')),
    h('div', { class: 'bench-compare' },
      col('对标', b.theirs.map((x) => ({ ...x, tag: h('span', { class: 'subtle' }, ` ${x.account} ×${x.burst}`) }))),
      col('我们', (b.ours || []).map((x) => ({ ...x, tag: x.lift != null ? h('span', { class: 'subtle' }, ` ${x.lift}×`) : null })))));
  }
  const st = b && b.structure;
  if (st && st.dims && st.dims.length) {
    const shares = (xs) => (xs && xs.length ? xs.map((x) => `${x.label} ${Math.round(x.share * 100)}%`).join(' · ') : '—');
    out.push(h('div', { class: 'subtle small' }, `开场结构:拆过的 ${st.n_theirs} 条对标爆款 vs 我们 ${st.n_ours} 条`
      + (st.claim_share != null ? `(对标爆款 ${Math.round(st.claim_share * 100)}% 开场就下判断)` : '')),
    h('div', { class: 'bench-compare' },
      h('div', { class: 'bench-col' }, h('div', { class: 'subtle small' }, '对标'),
        ...st.dims.map((d) => h('div', { class: 'small' }, h('strong', {}, `${d.label} `), shares(d.theirs)))),
      h('div', { class: 'bench-col' }, h('div', { class: 'subtle small' }, '我们'),
        ...st.dims.map((d) => h('div', { class: 'small' }, h('strong', {}, `${d.label} `), d.dimension === 'cover' ? '没记' : shares(d.ours))))));
  }
  return out;
}

// ---------------------------------------------------------------------------
// 行业日历 + 对标账号
// ---------------------------------------------------------------------------
function eventRow(e) {
  const days = e.in_days === 0 ? badge('今天', 'danger') : badge(`${e.in_days} 天后`, e.in_days <= 7 ? 'warning' : 'neutral');
  return h('div', { class: 'rowline' },
    h('span', { class: 'num subtle' }, e.day.slice(5)), days, badge(e.kind_label, 'brand'),
    h('span', { class: 'grow small' }, e.car && !e.title.includes(e.car) ? h('strong', {}, `${e.car} `) : null, e.title),
    e.plan_item_id ? badge('已出快反', 'success') : null,
    e.url ? h('a', { class: 'subtle', href: e.url, target: '_blank', rel: 'noopener', title: '来源' }, icon('external', 'i-sm'))
      : h('span', { class: 'subtle small' }, e.source === 'manual' ? '手填' : ''),
    h('button', {
      class: 'btn btn-sm btn-ghost', type: 'button', title: '不算这个事件',
      onclick: (ev) => withLoading(ev.currentTarget, () => call(`/api/team/events/${e.id}/dismiss`, '已去掉')),
    }, icon('x', 'i-sm')));
}

function benchRow(a) {
  const name = a.nickname || a.note || `${a.id.slice(0, 14)}…`;
  const state = a.last_error
    ? h('span', { class: 'small text-danger', title: a.last_error }, icon('alert-circle', 'i-sm'), ' 上次失败')
    : h('span', { class: 'subtle small' }, a.last_fetched_at ? `${relTime(a.last_fetched_at)}看过` : '还没看');
  return h('div', { class: `rowline${a.enabled ? '' : ' is-off'}` },
    h('a', { class: 'grow', href: a.url || `https://www.douyin.com/user/${a.id}`, target: '_blank', rel: 'noopener' }, name),
    a.note && a.nickname ? h('span', { class: 'subtle small' }, a.note) : null,
    h('span', { class: 'subtle small num' }, `${a.posts} 条 · 中位赞 ${Math.round(a.median_likes || 0)}`),
    a.hits ? badge(`爆款 ${a.hits}`, 'warning') : null,
    state,
    h('button', {
      class: 'btn btn-sm btn-ghost', type: 'button', title: a.enabled ? '暂停看这个号' : '恢复看这个号',
      onclick: (ev) => withLoading(ev.currentTarget, () =>
        call(`/api/team/benchmark/accounts/${a.id}/toggle`, a.enabled ? '已暂停' : '已恢复', { enabled: !a.enabled })),
    }, icon(a.enabled ? 'pause' : 'play', 'i-sm')),
    h('button', {
      class: 'btn btn-sm btn-ghost', type: 'button', title: '删掉(连同抓到的作品)',
      onclick: (ev) => withLoading(ev.currentTarget, async () => {
        const yes = await confirmDialog({ title: `删掉对标账号「${name}」?`, body: '抓到的作品数据一起删,之后可以再加。', confirmText: '删掉', tone: 'danger' });
        if (!yes) return;
        await api.del(`/api/team/benchmark/accounts/${a.id}`).catch((err) => toast(err.message, { type: 'danger' }));
        await load();
      }),
    }, icon('trash', 'i-sm')));
}

function benchHit(p) {
  return h('div', { class: 'rowline' },
    badge(`×${p.burst}`, 'warning'),
    h('a', { class: 'grow small', href: `https://www.douyin.com/video/${p.id}`, target: '_blank', rel: 'noopener', title: p.title },
      p.title.slice(0, 40) || p.id),
    p.car ? badge(p.car, 'brand') : null,
    h('span', { class: 'subtle small num' }, `${p.account} · 赞 ${p.likes} · ${Math.round(p.duration)}s`));
}

function renderSide() {
  const kinds = DATA.event_kinds || {};
  const sel = $('#event-form select');
  if (!sel.options.length) sel.replaceChildren(...Object.entries(kinds).map(([k, v]) => h('option', { value: k }, v)));
  const evs = DATA.events || [];
  $('#event-list').replaceChildren(
    ...(evs.length ? evs.map(eventRow) : [empty('未来 30 天还没有事件。行业日历每批新闻后自动挑,也可以手填。')]));
  const bench = DATA.bench || { accounts: [], hits: [] };
  $('#bench-count').textContent = String(bench.accounts.length);
  $('#bench-list').replaceChildren(
    ...(bench.accounts.length ? bench.accounts.map(benchRow) : [empty('还没有对标账号。贴 10–20 个同类号的主页链接,每天看一遍。')]));
  $('#bench-hits').replaceChildren(
    ...(bench.hits.length
      ? [h('div', { class: 'subtle small' }, `近 14 天对标爆款(点赞 ≥ 该号中位数 ${bench.burst_ratio} 倍)`), ...bench.hits.map(benchHit)]
      : []));
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
  const bench = l.source === 'benchmark';
  return h('div', { class: `rowline${off || l.shadowed ? ' is-off' : ''}` },
    badge(l.effect >= 1 ? '多做' : '少做', l.effect >= 1 ? 'success' : 'warning'),
    bench ? h('span', { title: '对标号的先验:倍数夹在 0.8–1.25;自己同一取值攒够 3 条就让位' }, badge('对标', 'brand')) : null,
    h('span', { class: 'grow small', title: ev }, bench ? l.text.replace(/^对标号 /, '') : l.text),
    l.shadowed ? badge('自己的数据已覆盖', 'neutral') : null,
    off ? badge('已停用', 'neutral') : null,
    btn);
}

// ---------------------------------------------------------------------------
// 评论待审(运营官)
// ---------------------------------------------------------------------------
const KIND_TONE = { question: 'brand', praise: 'success', argue: 'warning', ad: 'danger', other: 'neutral' };
const KIND_LABEL = { question: '问题', praise: '夸', argue: '杠', ad: '广告', other: '其他' };
const REPLY_STATE = {
  draft: ['待审', 'warning'], approved: ['已批,待发', 'brand'], sending: ['发送中', 'brand'],
  failed: ['发送失败', 'danger'],
};
const PICKED = new Set();

function pickedButtons() {
  $('#btn-approve-picked').disabled = !PICKED.size;
  $('#btn-skip-picked').disabled = !PICKED.size;
}

function commentRow(c) {
  const locked = c.reply_state === 'sending';
  const pick = h('input', {
    type: 'checkbox', checked: PICKED.has(c.id), disabled: locked || c.reply_state === 'approved',
    onchange: (e) => { e.target.checked ? PICKED.add(c.id) : PICKED.delete(c.id); pickedButtons(); },
  });
  const kind = h('select', {
    class: 'select comment-kind', disabled: locked, title: '分错了就改',
    onchange: (e) => call('/api/team/comments/edit', '已改分类', { id: c.id, kind: e.target.value }),
  }, Object.entries(KIND_LABEL).map(([k, v]) => h('option', { value: k, selected: k === c.kind }, v)));
  const reply = h('textarea', {
    class: 'input comment-reply', rows: '2', maxlength: '80', disabled: locked,
    placeholder: c.kind === 'ad' ? '广告不回' : '写回复',
  }, c.reply_draft || '');
  reply.addEventListener('change', () =>
    call('/api/team/comments/edit', '回复已改,批准后才发', { id: c.id, reply: reply.value }));
  const act = (label, cls, fn, ic, title) =>
    h('button', { class: `btn btn-sm ${cls}`, type: 'button', title, disabled: locked,
      onclick: (e) => withLoading(e.currentTarget, fn) }, ic ? icon(ic, 'i-sm') : null, label);
  const decide = (action, ok) => call('/api/team/comments/decide', ok, { ids: [c.id], action });
  const [stateText, stateTone] = REPLY_STATE[c.reply_state] || [c.reply_state, 'neutral'];
  const pooled = c.topic_state !== 'dismissed';
  return h('div', { class: 'comment-row' },
    pick,
    h('div', { class: 'grow comment-main' },
      h('div', { class: 'row comment-meta small' },
        kind,
        h('strong', {}, c.author || '(匿名)'),
        h('span', { class: 'subtle' }, `${relTime(c.created_at)} · 《${c.work_title || ''}》`),
        c.likes ? h('span', { class: 'subtle' }, `${c.likes} 赞`) : null,
        badge(stateText, stateTone),
        c.reply_source === 'llm' ? badge('AI 拟', 'neutral') : c.reply_source === 'user' ? badge('你写的', 'neutral') : null),
      h('div', { class: 'comment-text' }, c.text || '(只有表情)'),
      c.reply_error ? h('div', { class: 'small text-danger' }, c.reply_error) : null,
      reply),
    h('div', { class: 'comment-actions' },
      c.reply_state === 'approved'
        ? act('撤回', 'btn-ghost', () => decide('reset', '已退回草稿'), 'refresh')
        : act('批准', 'btn-primary', () => decide('approve', '已批准,运营官下一轮发'), 'check'),
      act('不回', 'btn-ghost', () => decide('skip', '这条不回了'), 'x'),
      c.kind === 'question'
        ? act(pooled ? '不进选题池' : '进选题池', 'btn-ghost',
            () => call('/api/team/comments/topic', pooled ? '不给主编看了' : '主编下一轮会看到', { id: c.id, pooled: !pooled }),
            pooled ? 'x' : 'plus', '观众问的会附在主编排选题的提示词里')
        : null));
}

function engageStats(s) {
  if (!s) return '';
  const k = s.kinds || {};
  const parts = [`近 ${s.days} 天 ${s.total} 条评论(问题 ${k.question || 0} · 夸 ${k.praise || 0} · 杠 ${k.argue || 0} · 广告 ${k.ad || 0})`];
  parts.push(`回复率 ${pct(s.reply_rate)}`);
  parts.push(`首小时互动率 ${pct(s.first_hour)}${s.first_hour_n ? `(${s.first_hour_n} 条)` : ''}`);
  if (s.idle_days != null && s.idle_days >= 3) parts.push(`⚠ 已 ${Math.floor(s.idle_days)} 天没发`);
  return parts.join(' · ');
}

// ---------------------------------------------------------------------------
// 迭代记录:按周小图(一张图一个指标)+ 实验进度 + 变更日志
// ---------------------------------------------------------------------------
const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

function wan(v) {
  return v >= 10000 ? `${(v / 10000).toFixed(1)} 万` : String(Math.round(v));
}

const TRENDS = [
  { key: 'median_views', label: '中位播放', fmt: wan, n: (w) => `${w.measured} 条有数` },
  { key: 'retention_2s', label: '2 秒留存', fmt: pct, n: (w) => `${w.measured} 条有数` },
  { key: 'avg_watch_sec', label: '平均观看', fmt: (v) => `${v.toFixed(1)} 秒`, n: (w) => `${w.measured} 条有数` },
  { key: 'followers', label: '涨粉', fmt: (v) => String(v), n: (w) => `${w.measured} 条有数` },
  { key: 'reject_rate', label: '你的驳回率', note: '越低越对口味', fmt: pct, n: (w) => `${w.decided} 条已表态` },
];

function weekDay(iso) {
  const [, m, d] = iso.split('-');
  return `${Number(m)}/${Number(d)}`;
}

/** 一个指标的小折线。没数的周断开(不连过去、不当 0),悬停吸附到最近的周。 */
function trendChart(weeks, spec) {
  const W = 240, H = 92, pad = { l: 6, r: 6, t: 10, b: 18 };
  const vals = weeks.map((w) => w[spec.key]);
  const have = vals.filter((v) => v != null);
  const latest = [...weeks].reverse().find((w) => w[spec.key] != null);
  const head = h('div', { class: 'iter-chart-head' },
    h('span', { class: 'iter-chart-title' }, spec.label),
    h('strong', { class: 'num' }, latest ? spec.fmt(latest[spec.key]) : '—'));
  const box = h('div', { class: 'iter-chart' }, head);
  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'chart-svg', role: 'img',
    'aria-label': `${spec.label},近 ${weeks.length} 周:${vals.map((v) => (v == null ? '无' : spec.fmt(v))).join('、')}`,
  });
  box.appendChild(svg);
  if (spec.note) box.appendChild(h('div', { class: 'subtle small' }, spec.note));
  if (!have.length) {
    const t = svgEl('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle', class: 'chart-empty' });
    t.textContent = '还没有数据';
    svg.appendChild(t);
    return box;
  }
  let min = Math.min(...have), max = Math.max(...have);
  if (min === max) { min -= Math.abs(min) * 0.1 || 1; max += Math.abs(max) * 0.1 || 1; }
  const x = (i) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(1, weeks.length - 1);
  const y = (v) => H - pad.b - ((v - min) / (max - min)) * (H - pad.t - pad.b);
  for (const gy of [pad.t, H - pad.b]) {
    svg.appendChild(svgEl('line', { x1: pad.l, y1: gy, x2: W - pad.r, y2: gy, class: 'chart-grid' }));
  }
  let seg = [];
  const flush = () => {
    if (seg.length > 1) {
      svg.appendChild(svgEl('path', {
        d: seg.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' '),
        class: 'chart-line',
      }));
    }
    seg = [];
  };
  vals.forEach((v, i) => (v == null ? flush() : seg.push([x(i), y(v)])));
  flush();
  vals.forEach((v, i) => {
    if (v != null) svg.appendChild(svgEl('circle', { cx: x(i), cy: y(v), r: 4, class: 'iter-dot' }));
  });
  for (const [i, anchor] of [[0, 'start'], [weeks.length - 1, 'end']]) {
    const t = svgEl('text', { x: x(i), y: H - 4, 'text-anchor': anchor, class: 'chart-label' });
    t.textContent = weekDay(weeks[i].start);
    svg.appendChild(t);
  }

  const cross = svgEl('line', { y1: pad.t, y2: H - pad.b, class: 'iter-cross', visibility: 'hidden' });
  svg.appendChild(cross);
  const tip = h('div', { class: 'iter-tip', hidden: true });
  box.appendChild(tip);
  const hit = svgEl('rect', { x: 0, y: 0, width: W, height: H, fill: 'transparent' });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(weeks.length - 1,
      Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (weeks.length - 1))));
    const w = weeks[i];
    cross.setAttribute('x1', x(i));
    cross.setAttribute('x2', x(i));
    cross.setAttribute('visibility', 'visible');
    tip.replaceChildren(
      h('strong', { class: 'num' }, w[spec.key] == null ? '无数据' : spec.fmt(w[spec.key])),
      h('div', { class: 'subtle' }, `${weekDay(w.start)} 那周 · 发 ${w.works} 条 · ${spec.n(w)}`));
    tip.hidden = false;
    tip.style.left = `${(x(i) / W) * 100}%`;
  });
  hit.addEventListener('mouseleave', () => {
    cross.setAttribute('visibility', 'hidden');
    tip.hidden = true;
  });
  return box;
}

function trendTable(weeks) {
  return h('details', { class: 'iter-table small' },
    h('summary', {}, '看表格'),
    h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, '周'), h('th', {}, '发了'),
        ...TRENDS.map((s) => h('th', {}, s.label)))),
      h('tbody', {}, ...[...weeks].reverse().map((w) => h('tr', {},
        h('td', {}, `${weekDay(w.start)} 起`), h('td', { class: 'num' }, String(w.works)),
        ...TRENDS.map((s) => h('td', { class: 'num' }, w[s.key] == null ? '—' : s.fmt(w[s.key]))))))));
}

const LOG_TONE = { weight: 'brand', experiment: 'warning', param: 'neutral', preference: 'success' };

function renderIteration() {
  const it = DATA.iteration || { weeks: [], experiments: [], log: [] };
  $('#iter-trends').replaceChildren(
    h('div', { class: 'iter-charts' }, ...TRENDS.map((s) => trendChart(it.weeks, s))),
    trendTable(it.weeks));
  $('#iter-exps').replaceChildren(h('h3', { class: 'iter-h' }, '实验进度'),
    ...(it.experiments.length ? it.experiments.map((e) => h('div', { class: 'iter-exp' },
      h('div', { class: 'row' }, h('span', { class: 'grow small' }, e.label),
        h('span', { class: 'subtle small num' }, e.done ? '已攒够,等复盘' : `${e.used}/${e.size} · 差 ${e.left} 条`)),
      h('div', { class: 'iter-bar', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': e.size,
        'aria-valuenow': e.used, 'aria-label': e.label },
      h('span', { style: `width:${Math.min(100, (e.used / Math.max(1, e.size)) * 100)}%` }))))
      : [h('div', { class: 'subtle small' }, '没有在跑的配对实验')]),
    h('h3', { class: 'iter-h iter-h-gap' }, '写脚本时带的经验'),
    it.script_hint
      ? h('pre', { class: 'iter-hint small' }, it.script_hint)
      : h('div', { class: 'subtle small' }, '还没有:至少 4 条有 2 秒留存的作品才给开场正反例'));
  $('#iter-log').replaceChildren(h('h3', { class: 'iter-h' }, '变更日志'),
    ...(it.log.length ? it.log.map((r) => h('div', { class: 'rowline' },
      h('span', { class: 'subtle small num' }, relTime(r.ts)),
      badge(r.kind_label, LOG_TONE[r.kind]),
      h('span', { class: 'grow small' }, r.title)))
      : [h('div', { class: 'subtle small' }, '还没有自动改动:权重变动超过 0.1、实验开始 / 结束都会记在这里')]));
}

// ---------------------------------------------------------------------------
// 你的审核偏好
// ---------------------------------------------------------------------------
async function prefCall(method, url, body, ok) {
  try {
    await api[method](url, body);
    if (ok) toast(ok);
    await load();
  } catch (err) {
    toast(err.message, { type: 'danger' });
  }
}

function editPreference(p) {
  const input = h('input', { class: 'input', value: p.text, maxlength: '60' });
  const save = h('button', { class: 'btn btn-primary', type: 'button' }, '保存');
  const cancel = h('button', { class: 'btn btn-ghost', type: 'button' }, '取消');
  const content = h('div', {},
    h('div', { class: 'modal-head' }, h('div', { class: 'modal-title' }, '改这条偏好')),
    h('div', { class: 'modal-body stack-sm' }, input,
      h('div', { class: 'subtle small' }, '改过的算你手写的,复盘官以后不会覆盖它')),
    h('div', { class: 'modal-foot' }, cancel, save));
  const { close } = openModal(content, { label: '改这条偏好' });
  cancel.addEventListener('click', () => close(false));
  save.addEventListener('click', () => withLoading(save, async () => {
    await prefCall('patch', `/api/team/preferences/${p.id}`, { text: input.value.trim() }, '已保存');
    close(true);
  }));
}

function preferenceRow(p) {
  const off = p.status === 'off';
  const btn = (label, ico, title, fn) => h('button', {
    class: 'btn btn-sm btn-ghost btn-icon', type: 'button', title: `${label}:${title}`, 'aria-label': label,
    onclick: (e) => withLoading(e.currentTarget, fn),
  }, icon(ico, 'i-sm'));
  return h('div', { class: `rowline${off ? ' is-off' : ''}` },
    badge(p.source === 'manual' ? '手写' : '归纳', p.source === 'manual' ? 'brand' : 'neutral'),
    h('span', { class: 'grow small' }, p.text),
    off ? badge('已停用', 'neutral') : null,
    btn('改', 'pen', '改写这条', async () => editPreference(p)),
    btn(off ? '恢复' : '停用', off ? 'play' : 'pause', off ? '恢复后重新带进提示词' : '停用的不带进提示词,复盘也不会再归纳出来',
      () => prefCall('patch', `/api/team/preferences/${p.id}`, { status: off ? 'active' : 'off' },
        off ? '已恢复' : '已停用')),
    p.source === 'manual'
      ? btn('删', 'trash', '删掉这条', () => prefCall('del', `/api/team/preferences/${p.id}`, undefined, '已删除'))
      : null);
}

function renderPreference() {
  const pf = DATA.preference || { items: [], rates: [], base: {}, rate_on: true };
  const add = h('input', { class: 'input grow', maxlength: '60', placeholder: '自己加一条,比如:标题别用问号结尾' });
  const addBtn = h('button', { class: 'btn btn-sm', type: 'button' }, icon('plus', 'i-sm'), '加');
  const submit = () => add.value.trim() && withLoading(addBtn,
    () => prefCall('post', '/api/team/preferences', { text: add.value.trim() }, '已加'));
  addBtn.addEventListener('click', submit);
  add.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  $('#pref-list').replaceChildren(
    h('h3', { class: 'iter-h' }, '带进提示词的偏好'),
    ...(pf.items.length ? pf.items.map(preferenceRow)
      : [h('div', { class: 'subtle small' }, '还没有:复盘官每周把你的驳回理由、改动、改标题归纳成 3–5 条(近 30 天至少 3 条有话可说的表态)')]),
    h('div', { class: 'row pref-add' }, add, addBtn));
  const base = pf.base || {};
  const toggle = h('button', {
    class: 'btn btn-sm btn-ghost', type: 'button',
    title: '主编排选题时乘上你对这类车型 / 片型的通过率(0.7–1.3)',
    onclick: (e) => withLoading(e.currentTarget, () => prefCall('post', '/api/team/preferences/rate',
      { enabled: !pf.rate_on }, pf.rate_on ? '已关:排序不看通过率' : '已开:排序乘上通过率')),
  }, icon(pf.rate_on ? 'pause' : 'play', 'i-sm'), pf.rate_on ? '停用' : '启用');
  $('#pref-rates').replaceChildren(
    h('div', { class: 'row' },
      h('h3', { class: 'iter-h grow' }, '通过率 → 排序乘数'), toggle),
    h('div', { class: 'subtle small num' },
      base.n ? `近 60 天 ${base.n} 次批 / 驳,通过 ${Math.round(base.rate * 100)}%` : '还没有批 / 驳记录'),
    ...(pf.rates.length ? pf.rates.map((r) => h('div', { class: `rowline${pf.rate_on ? '' : ' is-off'}` },
      badge(r.mult >= 1 ? '常批' : '常驳', r.mult >= 1 ? 'success' : 'warning'),
      h('span', { class: 'grow small' }, r.label),
      h('span', { class: 'subtle small num' }, `批 ${r.approved} / ${r.n}`),
      h('span', { class: 'small num' }, `×${r.mult.toFixed(2)}`)))
      : [h('div', { class: 'subtle small' }, '同一车型 / 片型至少表态 3 次,而且和你的平均通过率有差别,才会出乘数')]));
}

async function decidePicked(action, ok) {
  const ids = [...PICKED];
  PICKED.clear();
  pickedButtons();
  await call('/api/team/comments/decide', ok, { ids, action });
}

function empty(text) {
  return h('div', { class: 'empty' }, h('div', { class: 'empty-title' }, text));
}

const ACTIVE_STATE = {
  done: ['完成', 'success'], failed: ['失败', 'danger'], skipped: ['跳过', 'neutral'],
  doing: ['进行中', 'neutral'], planned: ['待做', 'neutral'],
};

function activeRow(r) {
  const [stateText, stateTone] = ACTIVE_STATE[r.state] || [r.state, 'neutral'];
  return h('div', { class: 'comment-row' },
    h('div', { class: 'grow comment-main' },
      h('div', { class: 'row comment-meta small' },
        badge(r.action_label || r.action, 'neutral'),
        h('strong', {}, r.author || (r.title ? r.title.slice(0, 18) : '(对标账号)')),
        h('span', { class: 'subtle' }, relTime(r.created_at)),
        badge(stateText, stateTone)),
      r.text ? h('div', { class: 'comment-text' }, r.text) : null,
      r.reason ? h('div', { class: 'small text-danger' }, r.reason) : null));
}

function activeStats(s) {
  if (!s) return '';
  const q = s.quotas || {};
  const c = s.counts || {};
  const parts = [`今天(截至 ${s.day})已评论 ${c.comment || 0}/${q.comment ?? 0}`,
    `点赞 ${c.like || 0}/${q.like ?? 0}`, `分享 ${c.share || 0}/${q.share ?? 0}`,
    `关注 ${c.follow || 0}/${q.follow ?? 0}`];
  parts.push(`计划 ${s.planned || 0} 个动作`);
  if (s.failed) parts.push(`⚠ ${s.failed} 次失败`);
  if (s.paused) parts.push('⚠ 今天已停手');
  if (!s.enabled) parts.push('已关闭');
  return parts.join(' · ');
}

function render() {
  // 快反不等周单:排在待批最前面
  const pending = DATA.plan.filter((i) => i.status === 'proposed')
    .sort((a, b) => (b.kind === 'rapid') - (a.kind === 'rapid'));
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
  const comments = DATA.comments || [];
  const ids = new Set(comments.map((c) => c.id));
  [...PICKED].forEach((id) => ids.has(id) || PICKED.delete(id));
  $('#comment-count').textContent = String(comments.filter((c) => c.reply_state === 'draft').length);
  $('#comment-list').replaceChildren(
    ...(comments.length ? comments.map(commentRow) : [empty('没有待审的评论。运营官每 2 小时拉一次团队作品的新评论。')]));
  $('#comment-stats').textContent = engageStats(DATA.engage);
  $('#auto-thanks').checked = !!(DATA.engage && DATA.engage.auto_thanks);
  const active = DATA.active || {};
  const activeItems = active.items || [];
  $('#active-count').textContent = String((active.counts || {}).comment || 0);
  $('#active-stats').textContent = activeStats(active);
  $('#active-enabled').checked = !!active.enabled;
  $('#active-list').replaceChildren(
    ...(activeItems.length ? activeItems.map(activeRow)
      : [empty('今天还没有对外互动记录。活跃官按计划低频去对标号的作品下评论 / 点赞 / 分享 / 关注。')]));
  pickedButtons();
  $('#plan-list').replaceChildren(...(rest.length ? rest.map(planRow) : [empty('还没有排上的选题')]));
  const s = DATA.settings;
  $('#plan-hint').textContent =
    `每天 ${s.daily_slots} 条,发布 ${s.slot_times.join('、')},提前 ${s.lead_hours} 小时开工,同车 ${s.cooldown_days} 天冷却`;
  renderSide();
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
  const exps = DATA.experiments || [];
  $('#experiment-box').replaceChildren(...(exps.length
    ? [...exps.map((exp) => h('div', { class: 'small' },
      h('strong', {}, `固定 ${exp.label || exp.value}`),
      h('span', { class: 'subtle' }, ` · 已发 ${exp.used}/${exp.size} 条`),
      exp.done
        ? h('span', { class: 'subtle' }, ' · 已攒够,下次复盘换下一个')
        : h('span', { class: 'subtle' }, ` · 还差 ${exp.left} 条`))),
    h('div', { class: 'subtle small' },
      '几个实验轮着来:一条片只固定其中一个,其余照常换 —— 每个攒够 3 条才比得出好坏')]
    : [empty('没有在跑的配对实验(复盘官每周日下发)')]));
  renderIteration();
  renderPreference();

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

$('#btn-approve-picked').addEventListener('click', (e) =>
  withLoading(e.currentTarget, () => decidePicked('approve', '已批准,运营官下一轮发(或点「现在拉评论 / 发已批的」)')));
$('#btn-skip-picked').addEventListener('click', (e) =>
  withLoading(e.currentTarget, () => decidePicked('skip', '这些不回了')));

$('#auto-thanks').addEventListener('change', async (e) => {
  const on = e.target.checked;
  if (on) {
    const yes = await confirmDialog({
      title: '打开感谢类自动回?',
      body: '「夸」类评论的模板感谢不再等你批,运营官拉到就以你的名义发出。问题 / 杠 / 其他仍要你批。',
      confirmText: '打开',
    });
    if (!yes) { e.target.checked = false; return; }
  }
  await call('/api/team/comments/auto-thanks', on ? '感谢类自动回已打开' : '已关闭,全部回复都等你批', { enabled: on });
});

$('#active-enabled').addEventListener('change', async (e) => {
  const on = e.target.checked;
  if (!on) {
    const yes = await confirmDialog({
      title: '停掉活跃官?',
      body: '停了就不再去别人的作品下评论 / 点赞 / 分享 / 关注。已做过的留痕还在。',
      confirmText: '停掉',
    });
    if (!yes) { e.target.checked = true; return; }
  }
  await call('/api/team/active/toggle', on ? '活跃官已启用' : '活跃官已停', { enabled: on });
});

function formSubmit(sel, url, ok) {
  const form = $(sel);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(form).entries());
    withLoading(form.querySelector('button[type=submit]'), async () => {
      const res = await api.post(url, body).catch((err) => toast(err.message, { type: 'danger' }));
      if (res) { toast(ok); form.reset(); }
      await load();
    });
  });
}
formSubmit('#event-form', '/api/team/events', '记上了');
formSubmit('#bench-form', '/api/team/benchmark/accounts', '加上了,对标分析员下一轮会看它');

$('#btn-pause').addEventListener('click', (e) =>
  withLoading(e.currentTarget, async () => {
    await call('/api/team/pause', DATA.paused ? '团队已恢复' : '团队已暂停', { paused: !DATA.paused });
  }));

load();
setInterval(() => {
  // 正在填成片审核表单 / 看片时别刷新,不然输入和播放进度都会丢
  const busy = document.querySelector('.modal-backdrop')
    || document.activeElement?.closest?.('.review-card, .comment-row, .event-form')
    || [...document.querySelectorAll('.review-card video')].some((v) => !v.paused);
  if (!document.hidden && !busy) load();
}, 15000);
