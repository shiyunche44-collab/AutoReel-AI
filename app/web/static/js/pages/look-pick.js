/* 视觉包:面板、数据卡、章节条、字幕、封面、BGM 的整套包装。看示例图,点一下就选定。
 *
 * 示例图是仓库里的静态文件(scripts/look_samples.py 生成)。渲染开始前能换;
 * 之后只显示当前用的是哪套 —— 换了等于整片重画。
 */
import { $, h, icon } from '../ui/dom.js';
import { toast } from '../ui/toast.js';
import { api } from '../api.js';

/** @param {string} pid */
export function initLookPicker(pid) {
  const card = $('#look-pick');
  if (!card) return;
  const grid = $('#look-grid', card);

  let looks = []; // [{id, label, blurb, concept}]
  let current = null;
  let editable = false;
  let portrait = false;
  let busy = false;

  const labelOf = (id) => (looks.find((l) => l.id === id) || {}).label || id;

  function paint() {
    $('#look-current', card).textContent = current ? `当前:${labelOf(current)}` : '';
    grid.replaceChildren(
      ...looks.map((l, n) => {
        const on = l.id === current;
        return h(
          'button',
          {
            class: `cs-tile${on ? ' is-on' : ''}`,
            type: 'button',
            'aria-pressed': String(on),
            disabled: !editable && !on,
            title: l.blurb,
            onclick: () => choose(l.id),
          },
          h(
            'div',
            { class: 'cs-thumb' },
            h('img', { src: `/static/looks/${l.id}${portrait ? '_v' : ''}.jpg`, alt: l.label, loading: 'lazy' }),
            h('span', { class: 'cs-check' }, icon('check', 'i-sm')),
          ),
          h('div', { class: 'cs-name' }, h('span', { class: 'num subtle' }, String(n + 1).padStart(2, '0')), l.label),
          h('div', { class: 'cs-desc subtle small' }, l.blurb),
        );
      }),
    );
  }

  async function choose(id) {
    if (busy || !editable || id === current) return;
    busy = true;
    try {
      const res = await api.post(`/api/projects/${pid}/look`, { look: id });
      current = res.look;
      paint();
      toast(`视觉包:${labelOf(id)}`, { type: 'success' });
    } catch (err) {
      toast(err.message, { type: 'danger', title: '换视觉包失败' });
    } finally {
      busy = false;
    }
  }

  (async () => {
    let st;
    try {
      st = await api.get(`/api/projects/${pid}/look`);
    } catch {
      return;
    }
    looks = st.options || [];
    current = st.look;
    editable = !!st.editable;
    portrait = !!st.portrait;
    card.classList.toggle('is-portrait', portrait);
    card.classList.toggle('is-locked', !editable);
    $('#look-hint', card).textContent = editable
      ? '建项目时按最近几条自动挑的,开始渲染前都能换;没单独选过概念动画模版的话,模版跟着换'
      : '渲染已经开始,视觉包不能再换';
    card.classList.remove('hidden');
    paint();
  })();
}
