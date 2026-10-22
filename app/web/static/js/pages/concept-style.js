/* 概念动画模版:看固定示例图,点一下就选定。每套模版的版式、动效、字体、配色都不一样。
 *
 * 示例图是仓库里的静态文件(同一份示例数据),不在运行时生成;项目渲染时才按选定模版出动画。
 * 渲染前选只是记下;审片(门禁 B)时换,只重渲概念动画那几拍。
 */
import { $, h, icon } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { confirmDialog } from '../ui/modal.js';
import { api } from '../api.js';

/**
 * 概念动画模版面板。返回 { currentName, hasConcepts };没有面板时返回 null。
 *
 * @param {string} pid
 * @param {{ onRerender?: () => void }} [opts]
 */
export function initConceptStyle(pid, { onRerender } = {}) {
  const card = $('#concept-style');
  if (!card) return null;
  const grid = $('#cs-grid', card);
  const atGateB = card.dataset.state === 'GATE_B';

  let styles = []; // [{id, name, desc, light, image, portrait, image_portrait}]
  let current = null; // 项目当前模版 id
  let portrait = false; // 竖屏项目:只有做了竖屏版式的模版能选
  let busy = false;

  const nameOf = (id) => (styles.find((s) => s.id === id) || {}).name || id;

  function paint() {
    $('#cs-current', card).textContent = current ? `当前:${nameOf(current)}` : '';
    grid.replaceChildren(
      ...styles.map((s, n) => {
        const on = s.id === current;
        const pending = portrait && !s.portrait;
        const file = portrait ? s.image_portrait : s.image;
        const img = file
          ? h('img', { src: `/api/concept-styles/image/${encodeURIComponent(file)}`, alt: s.name, loading: 'lazy' })
          : h('div', { class: 'cs-ph' }, pending ? '竖屏版制作中' : '暂无示例图');
        return h(
          'button',
          {
            class: `cs-tile${on ? ' is-on' : ''}${pending ? ' is-pending' : ''}`,
            type: 'button',
            'aria-pressed': String(on),
            disabled: pending && !on,
            title: pending ? '这套模版还没有竖屏版式' : s.desc,
            onclick: () => choose(s.id),
          },
          h('div', { class: 'cs-thumb' }, img, h('span', { class: 'cs-check' }, icon('check', 'i-sm'))),
          h('div', { class: 'cs-name' }, h('span', { class: 'num subtle' }, String(n + 1).padStart(2, '0')), s.name),
          h('div', { class: 'cs-desc subtle small' }, s.desc),
        );
      }),
    );
  }

  async function choose(sid) {
    if (busy || sid === current) return;
    if (atGateB) {
      const ok = await confirmDialog({
        title: `换成「${nameOf(sid)}」?`,
        body: '概念动画那几拍会用新模版重渲,其他拍不动,渲完自动回到审片。',
        confirmText: '换模版并重渲',
        tone: 'success',
      });
      if (!ok) return;
    }
    busy = true;
    try {
      const res = await api.post(`/api/projects/${pid}/concept-style`, { style: sid });
      current = res.style;
      paint();
      const redo = (res.rerender || []).length;
      toast(redo ? `已换成「${nameOf(sid)}」,正在重渲 ${redo} 拍概念动画` : `概念动画模版:${nameOf(sid)}`, {
        type: 'success',
      });
      if (redo && onRerender) onRerender();
    } catch (err) {
      toast(err.message, { type: 'danger', title: '换模版失败' });
    } finally {
      busy = false;
    }
  }

  (async () => {
    let st;
    let g;
    try {
      [st, g] = await Promise.all([api.get(`/api/projects/${pid}/concept-style`), api.get('/api/concept-styles')]);
    } catch {
      return;
    }
    if (!st.beats) return;
    current = st.style;
    portrait = !!st.portrait;
    styles = g.styles || [];
    card.classList.toggle('is-portrait', portrait);
    card.classList.remove('hidden');
    $('#cs-hint', card).textContent = `本片 ${st.beats} 拍概念动画`;
    $('#cs-intro', card).textContent = atGateB
      ? '示例图是同一份示例数据的效果示意。换模版只重渲概念动画那几拍,其他拍不动。'
      : '20 套模版版式和动效各不相同,示例图是同一份示例数据的效果示意。点一下就选定,确认渲染时按选定的模版出动画;不选就用「实车标注」。';
    paint();
  })();

  return {
    currentName: () => (current ? nameOf(current) : ''),
    hasConcepts: () => !card.classList.contains('hidden'),
  };
}
