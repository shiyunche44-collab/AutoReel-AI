/* aimovice 工作台交互。
   零构建链:直接用原生 DOM API,没有框架依赖。 */
(function () {
  'use strict';

  const PID = window.PID;
  if (!PID) return; // 首页无项目上下文,跳过

  const $ = (sel) => document.querySelector(sel);
  const feedback = $('#action-feedback');

  function say(msg, isError) {
    if (!feedback) return;
    feedback.textContent = msg || '';
    feedback.style.color = isError ? 'var(--err)' : 'var(--muted)';
    clearTimeout(say._t);
    if (msg && !isError) {
      say._t = setTimeout(() => { feedback.textContent = ''; }, 6000);
    }
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined
    });
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  }

  // ---------- 推进 / 门禁 ----------
  async function act(url, body, label) {
    say(label + ' …');
    try {
      const data = await post(url, body);
      if (data.state && data.state !== window.__lastState) {
        say(`${label}完成 → ${data.state}`);
        setTimeout(() => location.reload(), 900);
      } else {
        say(data.reason || data.action || `${label}完成`);
        refreshStatus();
      }
    } catch (err) {
      say(err.message, true);
    }
  }

  const btnAdvance = $('#btn-advance');
  if (btnAdvance) {
    btnAdvance.addEventListener('click', () => act(`/api/projects/${PID}/advance`, null, '推进'));
  }
  const gateA = $('#btn-gate-a');
  if (gateA) {
    gateA.addEventListener('click', async () => {
      if (!confirm('确认脚本与分镜无误？下一步会开始生成草稿镜头并消耗套餐额度。')) return;
      await act(`/api/projects/${PID}/gate`, { gate: 'A' }, '确认门禁 A');
    });
  }
  const gateB = $('#btn-gate-b');
  if (gateB) {
    gateB.addEventListener('click', async () => {
      // 分批审片:只放行当前批次的镜头,审完一批就能开跑一批,
      // 不用等 40 条草稿全看完。
      const batch = await currentBatch();
      if (batch) {
        const msg =
          `批准第 ${batch.index + 1} 批(镜头 ${batch.shots[0]}–${batch.shots[batch.shots.length - 1]},` +
          `共 ${batch.shots.length} 个)并开始成片生成？\n` +
          `H3 单任务 15-30 分钟且消耗套餐额度。\n` +
          `还有 ${batch.totalPending} 个镜头在后续批次,本批完成后会回到门禁 B 继续审。`;
        if (!confirm(msg)) return;
        await act(`/api/projects/${PID}/gate`,
          { gate: 'B', approved: batch.shots }, '批准本批');
        return;
      }
      // 没有批次信息(旧项目或接口异常)→ 退回一次性全批通过
      if (!confirm('确认草稿通过？下一步会用 H3 生成成片镜头，单任务需 15-30 分钟且消耗套餐额度。')) return;
      await act(`/api/projects/${PID}/gate`, { gate: 'B' }, '确认门禁 B');
    });
  }

  // 取"下一个待审批次":优先第一个尚未整批批准的批次
  async function currentBatch() {
    try {
      const res = await fetch(`/api/projects/${PID}/review`);
      if (!res.ok) return null;
      const rv = await res.json();
      const next = (rv.batch_list || []).find(b => b.pending.length > 0);
      if (!next) return null;
      return {
        index: next.index,
        shots: next.shots,
        totalPending: rv.pending_total,
        totalBatches: rv.total_batches,
        cursor: rv.cursor,
      };
    } catch (err) {
      return null;
    }
  }

  // 门禁 B 时把批次进度显示出来
  async function renderReviewHint() {
    const el = $('#gate-hint');
    if (!el) return;
    const rv = await currentBatch();
    if (!rv) return;
    el.textContent =
      `分批审片:第 ${rv.index + 1}/${rv.totalBatches} 批待审` +
      `(本批 ${rv.shots.length} 镜头,共 ${rv.totalPending} 个待审)`;
  }
  renderReviewHint();

  // ---------- 保存脚本 ----------
  const saveScript = $('#btn-save-script');
  if (saveScript) {
    saveScript.addEventListener('click', async () => {
      const rows = Array.from(document.querySelectorAll('.script-row'));
      const segments = rows.map((row) => ({
        idx: parseInt(row.dataset.idx, 10),
        section: row.querySelector('.script-section').textContent.trim(),
        text: row.querySelector('.script-text').value.trim(),
        fact_refs: Array.from(row.querySelectorAll('.script-refs code'))
          .map((c) => c.textContent.trim())
      }));
      saveScript.disabled = true;
      say('保存中 …');
      try {
        const res = await fetch(`/api/projects/${PID}/script`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segments })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || '保存失败');
        say(`已保存 ${data.segments} 段`);
      } catch (err) {
        say(err.message, true);
      } finally {
        saveScript.disabled = false;
      }
    });
  }

  // ---------- 单镜头重拍 ----------
  document.querySelectorAll('.js-retry').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const idx = btn.dataset.idx;
      if (!confirm(`重拍镜头 ${idx}?这会重新生成该镜头并消耗额度。`)) return;
      try {
        await post(`/api/projects/${PID}/shots/${idx}/retry`, { stage: 'final' });
        say(`镜头 ${idx} 已重新排队`);
        setTimeout(() => location.reload(), 800);
      } catch (err) {
        say(err.message, true);
      }
    });
  });

  // ---------- 状态轮询 ----------
  let lastState = null;
  window.__lastState = null;

  async function refreshStatus() {
    try {
      const res = await fetch(`/api/projects/${PID}/status`);
      if (!res.ok) return;
      const s = await res.json();

      const badge = $('#state-badge');
      if (badge && badge.textContent !== s.state) {
        badge.textContent = s.state;
        badge.className = 'state state-' + s.state;
        window.__lastState = s.state;
        // 状态跃迁可能意味着有新产物,刷新一次拿最新画面
        if (lastState && lastState !== s.state) setTimeout(() => location.reload(), 1200);
      }
      lastState = s.state;

      const summary = $('#gen-summary');
      if (summary) {
        const c = s.counts || {};
        const parts = [`共 ${s.total}`];
        if (c.succeeded) parts.push(`成功 ${c.succeeded}`);
        if (c.submitted) parts.push(`已提交 ${c.submitted}`);
        if (c.running) parts.push(`生成中 ${c.running}`);
        if (c.queued) parts.push(`排队 ${c.queued}`);
        if (c.pending) parts.push(`待提交 ${c.pending}`);
        if (c.failed) parts.push(`失败 ${c.failed}`);
        summary.textContent = parts.join(' · ');
      }

      const hint = $('#gate-hint');
      if (hint) {
        hint.textContent = s.state_reason ||
          (s.state.startsWith('GATE') ? '等待你的确认后才能继续生成' : '');
      }
    } catch { /* 轮询失败静默重试 */ }
  }

  setInterval(refreshStatus, 5000);
  refreshStatus();
})();
