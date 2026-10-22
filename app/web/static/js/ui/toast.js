/* 轻提示。替代原来的 alert() 与操作条里那行小字。 */
import { h, icon } from './dom.js';

const ICONS = { success: 'check-circle', danger: 'alert-circle', warning: 'alert-triangle', brand: 'info' };

function stack() {
  let el = document.getElementById('toast-stack');
  if (!el) {
    el = h('div', { class: 'toast-stack', id: 'toast-stack', role: 'status', 'aria-live': 'polite' });
    document.body.appendChild(el);
  }
  return el;
}

/**
 * toast('已保存') / toast('失败原因', { type: 'danger' })
 * type: success | danger | warning | brand;错误默认停留更久,可传 duration: 0 常驻。
 * action: { label, onClick } 会渲染一个按钮。
 */
export function toast(message, opts = {}) {
  const type = opts.type || 'success';
  const duration = opts.duration ?? (type === 'danger' ? 8000 : 3800);
  const body = h(
    'div',
    { class: 'toast-body' },
    opts.title ? h('div', { class: 'toast-title' }, opts.title) : null,
    h('div', {}, message),
  );
  if (opts.action) {
    body.appendChild(
      h(
        'button',
        {
          class: 'btn btn-sm',
          type: 'button',
          style: 'margin-top:8px',
          onclick: () => {
            opts.action.onClick();
            close();
          },
        },
        opts.action.label,
      ),
    );
  }
  const closeBtn = h('button', { class: 'toast-close', type: 'button', 'aria-label': '关闭' }, icon('x', 'i-sm'));
  const el = h('div', { class: `toast tone-${type}` }, icon(ICONS[type] || 'info'), body, closeBtn);

  let timer;
  function close() {
    clearTimeout(timer);
    el.classList.add('is-leaving');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }
  closeBtn.addEventListener('click', close);
  if (duration > 0) {
    timer = setTimeout(close, duration);
    el.addEventListener('mouseenter', () => clearTimeout(timer));
    el.addEventListener('mouseleave', () => {
      timer = setTimeout(close, 1800);
    });
  }
  const s = stack();
  s.appendChild(el);
  // 最多保留 4 条,太多就挤掉最旧的
  while (s.children.length > 4) s.firstElementChild.remove();
  return close;
}
