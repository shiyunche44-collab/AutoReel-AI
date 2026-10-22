/* 分发台:账号管理、连通性自检、批量发布、队列轮询。 */
import { $, $$, readJSON, withLoading, h } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { confirmDialog } from '../ui/modal.js';
import { initChrome } from '../ui/chrome.js';
import { api } from '../api.js';

initChrome();

const DATA = readJSON('page-data', {});
const PLATFORMS = DATA.platforms || [];
const ACCOUNTS = DATA.accounts || [];

/* 各平台要填哪些凭据。空数组 = 扫码登录就行。 */
const CRED_FIELDS = {
  bilibili: [
    { key: 'cookie', label: 'Cookie', type: 'textarea',
      placeholder: 'SESSDATA=xxx; bili_jct=yyy',
      help: '浏览器 F12 → Network → 任意请求 → Request Headers 里复制整条 Cookie' },
  ],
  douyin: [
    { key: 'access_token', label: 'access_token', help: '开放平台 OAuth 换到的' },
    { key: 'open_id', label: 'open_id' },
    { key: 'client_key', label: 'client_key(可留空,读 .env)' },
    { key: 'client_secret', label: 'client_secret(可留空,读 .env)' },
  ],
  youtube: [
    { key: 'client_id', label: 'client id' },
    { key: 'client_secret', label: 'client secret' },
    { key: 'refresh_token', label: 'refresh token' },
  ],
  xiaohongshu: [],
  channels: [],
};

const STATE_LABEL = {
  pending: '排队中', uploading: '上传中', submitted: '已提交待审',
  published: '已发布', failed: '失败',
};
const STATE_TONE = {
  pending: 'neutral', uploading: 'brand', submitted: 'warning',
  published: 'success', failed: 'danger',
};

function say(message, type = 'success') {
  toast(message, { type });
}

/* ---------------------------------------------------------------------------
 * 账号表单
 * --------------------------------------------------------------------------- */
const formWrap = $('#account-form-wrap');
const accPlatform = $('#acc-platform');
const accName = $('#acc-name');
const accMeta = $('#acc-meta');
const accFields = $('#acc-cred-fields');
const accHint = $('#acc-hint');
let editingId = null;

function renderCredFields() {
  const key = accPlatform.value;
  const spec = CRED_FIELDS[key] || [];
  accFields.innerHTML = '';
  for (const field of spec) {
    const input = field.type === 'textarea'
      ? h('textarea', { class: 'textarea', name: field.key, rows: 3, placeholder: field.placeholder || '' })
      : h('input', { class: 'input', name: field.key, type: 'text', placeholder: field.placeholder || '' });
    accFields.appendChild(h('label', { class: 'field' },
      h('span', { class: 'field-label' }, field.label),
      input,
      field.help ? h('span', { class: 'field-help' }, field.help) : null));
  }
  const rule = PLATFORMS.find((p) => p.key === key);
  let hint = rule ? (rule.auth_hint || rule.note || '') : '';
  if ((key === 'xiaohongshu' || key === 'channels') && !spec.length) {
    hint = '这个平台靠扫码登录:保存账号后点「登录」,浏览器打开后扫码,关掉窗口即可。';
  }
  accHint.textContent = hint;
}

function openForm(account) {
  editingId = account ? account.id : null;
  formWrap.open = true;
  accPlatform.value = account ? account.platform : (PLATFORMS[0] || {}).key;
  accName.value = account ? account.name : '';
  accMeta.value = account && account.meta && Object.keys(account.meta).length
    ? JSON.stringify(account.meta) : '';
  renderCredFields();
  if (account) {
    // 凭据读回来是打码的,这里只提示"留空表示不修改",不假装能编辑原值
    for (const field of CRED_FIELDS[account.platform] || []) {
      const input = accFields.querySelector(`[name="${field.key}"]`);
      if (input) input.placeholder = '留空表示不修改(已配置的凭据会保留)';
    }
  }
  accName.focus();
}

if (accPlatform) accPlatform.addEventListener('change', renderCredFields);
$('#acc-cancel')?.addEventListener('click', () => { formWrap.open = false; editingId = null; });
formWrap?.addEventListener('toggle', () => { if (formWrap.open && !editingId) renderCredFields(); });

$('#account-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const credential = {};
  for (const field of CRED_FIELDS[accPlatform.value] || []) {
    const input = accFields.querySelector(`[name="${field.key}"]`);
    const value = input ? input.value.trim() : '';
    if (value) credential[field.key] = value;
  }
  let meta = {};
  if (accMeta.value.trim()) {
    try { meta = JSON.parse(accMeta.value); }
    catch { say('平台设置不是合法 JSON', 'danger'); return; }
  }
  const body = {
    id: editingId || undefined,
    platform: accPlatform.value,
    name: accName.value.trim() || accPlatform.value,
    credential,
    meta,
  };
  // 编辑时没填凭据 = 保留原来的
  if (editingId && !Object.keys(credential).length) delete body.credential;
  await withLoading($('#acc-save'), async () => {
    try {
      await api.post('/api/publish/accounts', body);
      say(editingId ? '账号已更新' : '账号已添加');
      formWrap.open = false;
      editingId = null;
      setTimeout(() => location.reload(), 400);
    } catch (err) {
      say(err.message, 'danger');
    }
  });
});

/* ---------------------------------------------------------------------------
 * 账号行操作
 * --------------------------------------------------------------------------- */
$('#account-list')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const act = btn.dataset.act;
  const id = btn.dataset.id;
  if (act === 'check') {
    await withLoading(btn, async () => {
      try {
        const r = await api.post(`/api/publish/accounts/${id}/check`);
        say(r.ok ? r.message : (r.hint || r.message), r.ok ? 'success' : 'danger');
      } catch (err) { say(err.message, 'danger'); }
    });
  } else if (act === 'del') {
    const ok = await confirmDialog({
      title: '删除这个账号?',
      body: '凭据也会一起删掉。已发布过的记录不受影响。',
      tone: 'danger', confirmText: '删除',
    });
    if (!ok) return;
    try {
      await api.del(`/api/publish/accounts/${id}`);
      say('已删除');
      setTimeout(() => location.reload(), 300);
    } catch (err) { say(err.message, 'danger'); }
  } else if (act === 'edit') {
    const account = ACCOUNTS.find((a) => a.id === id);
    if (account) openForm(account);
  } else if (act === 'login') {
    try {
      const r = await api.post(`/api/publish/login/${btn.dataset.platform}`);
      say(r.message, 'brand');
    } catch (err) { say(err.message, 'danger'); }
  }
});

/* 凭据摘要:只显示"哪些键配了",不显示值 */
$$('#account-list [data-cred]').forEach((el) => {
  let cred = {};
  try { cred = JSON.parse(el.dataset.cred || '{}'); } catch { cred = {}; }
  const keys = Object.keys(cred);
  el.textContent = keys.length ? `凭据:${keys.join('、')}` : '未配置凭据';
});

/* ---------------------------------------------------------------------------
 * 批量发布
 * --------------------------------------------------------------------------- */
const batchBox = $('#batch-platforms');
const batchNote = $('#batch-note');

function renderBatch() {
  if (!batchBox) return;
  batchBox.innerHTML = '';
  const byPlatform = new Map();
  for (const a of ACCOUNTS) {
    if (!a.enabled) continue;
    if (!byPlatform.has(a.platform)) byPlatform.set(a.platform, []);
    byPlatform.get(a.platform).push(a);
  }
  if (!byPlatform.size) {
    batchBox.appendChild(h('p', { class: 'subtle small' }, '还没有可用账号,先在上面添加一个。'));
    return;
  }
  for (const rule of PLATFORMS) {
    const list = byPlatform.get(rule.key) || [];
    const check = h('input', { type: 'checkbox', value: rule.key, class: 'pub-check' });
    if (!list.length) check.disabled = true;
    batchBox.appendChild(h('label', { class: 'pub-pick' },
      check,
      h('span', { class: 'pub-pick-name' }, rule.label),
      h('span', { class: 'subtle small' }, `${rule.aspect} · ${list.length} 个账号`)));
  }
  updateBatchNote();
}

function updateBatchNote() {
  if (!batchNote) return;
  const project = $('#batch-project');
  if (!project) return;
  const opt = project.selectedOptions[0];
  const vertical = opt && opt.dataset.vertical === '1';
  const picked = $$('.pub-check:checked').length;
  batchNote.textContent = picked
    ? `将发到 ${picked} 个平台${vertical ? '' : '(竖版还在生成,发布时会自动补)'}`
    : '先勾选要发的平台';
}

if (batchBox) {
  renderBatch();
  batchBox.addEventListener('change', updateBatchNote);
  $('#batch-project')?.addEventListener('change', updateBatchNote);
}

$('#batch-publish')?.addEventListener('click', async (e) => {
  const project = $('#batch-project');
  const platforms = $$('.pub-check:checked').map((c) => c.value);
  if (!project || !platforms.length) { say('先选项目和平台', 'warning'); return; }
  const accountIds = ACCOUNTS.filter((a) => a.enabled && platforms.includes(a.platform)).map((a) => a.id);
  const ok = await confirmDialog({
    title: '发布到这些平台?',
    body: `${platforms.length} 个平台、${accountIds.length} 个账号。发布后不可撤回,各平台会各自走一遍审核。`,
    confirmText: '发布',
  });
  if (!ok) return;
  await withLoading(e.currentTarget, async () => {
    try {
      const r = await api.post(`/api/projects/${project.value}/publish`, { account_ids: accountIds });
      const n = (r.created || []).length;
      const s = (r.skipped || []).length;
      say(`已排入 ${n} 条${s ? `,跳过 ${s} 条(已发过)` : ''}`);
      setTimeout(() => location.reload(), 600);
    } catch (err) { say(err.message, 'danger'); }
  });
});

/* ---------------------------------------------------------------------------
 * 队列
 * --------------------------------------------------------------------------- */
$('#queue-table')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-act="retry"]');
  if (!btn) return;
  await withLoading(btn, async () => {
    try {
      const r = await api.post(`/api/publish/${btn.dataset.id}/retry`);
      say(r.ok ? '已重新排队' : (r.message || '重试未成功'), r.ok ? 'success' : 'warning');
      setTimeout(() => location.reload(), 600);
    } catch (err) { say(err.message, 'danger'); }
  });
});

/* 队列轮询:上传要几分钟,光刷新页面看不到进展 */
function paintQueue(rows) {
  const tbody = $('#queue-table tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  for (const q of rows) {
    const badge = h('span', { class: `badge tone-${STATE_TONE[q.state] || 'neutral'}` },
      STATE_LABEL[q.state] || q.state);
    const result = q.remote_url
      ? h('a', { href: q.remote_url, target: '_blank', rel: 'noopener' }, '查看')
      : (q.error ? h('span', { class: 'text-danger' }, q.error) : h('span', { class: 'subtle' }, '—'));
    const tr = h('tr', { 'data-pub': q.id },
      h('td', {}, h('span', { class: 'badge tone-brand' }, q.platform_label)),
      h('td', { class: 'pub-title', title: q.title }, q.title),
      h('td', { class: 'subtle' }, q.aspect),
      h('td', {}, badge),
      h('td', { class: 'num' }, String(q.attempts || 0)),
      h('td', { class: 'subtle small' }, result),
      h('td', {}, h('button', {
        class: 'btn btn-sm btn-ghost', type: 'button', 'data-act': 'retry', 'data-id': q.id,
      }, '重试')));
    tbody.appendChild(tr);
  }
}

async function pollQueue() {
  try {
    const rows = await api.get('/api/publish/queue');
    paintQueue(rows);
  } catch { /* 忽略 */ }
}

setInterval(pollQueue, 15000);
