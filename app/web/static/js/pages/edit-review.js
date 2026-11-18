/* 门禁 B 整片预览(剪辑 v2):成片的取材和刀口在进门禁 B 前就定好了(edit_timeline.json),
 * 这里放半尺寸预览 + 刀表。点一行跳到那一刀;「换一刀」排除这一刀现在的素材重新取,
 * 后台再出一版预览(任务在跑时轮询,出好了自动换上)。
 */
import { $, h, icon, withLoading } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { api } from '../api.js';

const POLL_MS = 8000;

/** 秒 → "1:05.3"。
 * @param {number} t */
export function clock(t) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}

/** 对题标签:兜底 / 对上了 / 没对上;不需要对题的刀返回 null。
 * @param {{need?: boolean, match?: string, via?: string}} c */
export function matchTag(c) {
  if (c.via === 'fallback') return { text: '兜底', tone: 'tone-warning' };
  if (!c.need) return null;
  return c.match ? { text: '对题', tone: 'tone-success' } : { text: '没对上', tone: 'tone-danger' };
}

/** @param {string} pid */
export function initEditReview(pid) {
  const card = $('#edit-review');
  if (!card) return;
  const video = /** @type {HTMLVideoElement} */ ($('#er-video', card));
  const list = $('#er-cuts', card);
  /** @type {number | null} */
  let shownAt = null;
  let timer = 0;
  /** @type {any[]} */
  let cuts = [];

  /** @param {number} t */
  function seek(t) {
    if (!video.src) return;
    video.currentTime = t + 0.05;
    video.play().catch(() => {});
  }

  function paintCuts() {
    list.replaceChildren(
      ...cuts.map((c) => {
        const tag = matchTag(c);
        const p0 = (c.pieces || [])[0] || {};
        const swap = h(
          'button',
          { class: 'btn btn-ghost btn-sm', type: 'button', title: '排除这一刀现在的素材,重新取一段' },
          icon('refresh', 'i-sm'),
          '换一刀',
        );
        swap.addEventListener('click', (ev) => {
          ev.stopPropagation();
          withLoading(swap, () => doSwap(c.key));
        });
        return h(
          'div',
          { class: 'er-cut', role: 'listitem', 'data-key': c.key, onclick: () => seek(c.start) },
          h('img', {
            src: `/api/projects/${pid}/edit/cuts/${encodeURIComponent(c.key)}/thumb.jpg`,
            alt: '',
            loading: 'lazy',
          }),
          h(
            'div',
            {},
            h('div', { class: 'er-text' }, c.text || '—'),
            h(
              'div',
              { class: 'er-meta subtle small num' },
              `${clock(c.start)}–${clock(c.end)}`,
              ` · #${String(c.beat).padStart(2, '0')}`,
              p0.kind === 'still' ? ' · 静图' : '',
              tag ? h('span', { class: `tag ${tag.tone}` }, tag.text) : null,
            ),
          ),
          swap,
        );
      }),
    );
  }

  video.addEventListener('timeupdate', () => {
    const t = video.currentTime;
    for (const row of list.children) {
      const c = cuts.find((x) => x.key === row.getAttribute('data-key'));
      row.classList.toggle('is-now', !!c && t >= c.start && t < c.end);
    }
  });

  /** @param {any} st */
  function paintPreview(st) {
    const pv = st.preview || {};
    const job = pv.job || {};
    const busy = job.state === 'queued' || job.state === 'running';
    const label = $('#er-state', card);
    if (busy) label.textContent = '渲染中';
    else if (job.state === 'failed') label.textContent = '预览失败';
    else if (pv.exists && !pv.fresh) label.textContent = '旧版本';
    else label.textContent = pv.exists ? '最新' : '';
    label.className = `tag ${job.state === 'failed' ? 'tone-danger' : busy ? 'tone-warning' : 'tone-success'}`;
    if (job.state === 'failed' && job.error) label.title = job.error;
    const m = st.match || {};
    $('#er-match', card).textContent = m.need
      ? `对题 ${m.hit}/${m.need} 刀 · 文字模型复选 ${m.llm || 0} · 兜底 ${m.fallback || 0}`
      : '';
    const has = !!pv.exists;
    $('#er-ph', card).classList.toggle('hidden', has);
    video.classList.toggle('hidden', !has);
    if (has && pv.rendered_at !== shownAt) {
      shownAt = pv.rendered_at;
      const t = video.currentTime || 0;
      video.src = `/api/projects/${pid}/edit/preview.mp4?v=${pv.rendered_at || 0}`;
      if (t) video.addEventListener('loadedmetadata', () => (video.currentTime = t), { once: true });
    }
    clearTimeout(timer);
    if (busy || (pv.enabled && !pv.fresh && job.state !== 'failed')) {
      timer = window.setTimeout(load, POLL_MS);
    }
  }

  async function load() {
    let st;
    try {
      st = await api.get(`/api/projects/${pid}/edit`);
    } catch {
      return;
    }
    if (!st.enabled || !(st.cuts || []).length) return;
    card.classList.remove('hidden');
    cuts = st.cuts;
    paintCuts();
    paintPreview(st);
  }

  /** @param {string} key */
  async function doSwap(key) {
    try {
      await api.post(`/api/projects/${pid}/edit/cuts/${encodeURIComponent(key)}/swap`, {});
      toast('换好了,正在重出预览', { type: 'success' });
    } catch (err) {
      toast(err.message, { type: 'danger', title: '换一刀失败' });
      return;
    }
    await load();
  }

  const rerender = $('#er-rerender', card);
  rerender.addEventListener('click', () =>
    withLoading(rerender, async () => {
      try {
        await api.post(`/api/projects/${pid}/edit/preview`, {});
      } catch (err) {
        toast(err.message, { type: 'danger', title: '重出预览失败' });
      }
      await load();
    }),
  );

  load();
}
