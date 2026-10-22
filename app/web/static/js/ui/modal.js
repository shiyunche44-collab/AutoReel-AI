/* 弹窗。替代原生 confirm(),支持 Esc / 点遮罩关闭和焦点回收。 */
import { h, icon } from './dom.js';

const TONE_ICON = { danger: 'alert-triangle', warning: 'alert-triangle', success: 'check-circle', brand: 'info' };

/**
 * 打开一个弹窗,content 是 Node。返回 { el, close }。
 * onClose(result) 在关闭时调用一次。
 *
 * @param {Node} content
 * @param {{ size?: string, label?: string, onClose?: (result: any) => void }} [opts]
 * @returns {{ el: HTMLElement, close: (result?: any) => void }}
 */
export function openModal(content, { size = '', onClose, label = '对话框' } = {}) {
  const prevFocus = /** @type {HTMLElement|null} */ (document.activeElement);
  const modal = h(
    'div',
    { class: `modal ${size}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': label },
    content,
  );
  const backdrop = h('div', { class: 'modal-backdrop' }, modal);
  let closed = false;

  function close(result) {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    backdrop.classList.add('is-leaving');
    backdrop.addEventListener('animationend', () => backdrop.remove(), { once: true });
    document.body.style.overflow = '';
    if (prevFocus && prevFocus.focus) prevFocus.focus({ preventScroll: true });
    if (onClose) onClose(result);
  }
  function onKey(e) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close(false);
    }
    if (e.key === 'Tab') {
      // 焦点限制在弹窗内
      const f = modal.querySelectorAll(
        'button, [href], input, textarea, select, video, [tabindex]:not([tabindex="-1"])',
      );
      if (!f.length) return;
      const first = f[0],
        last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
  backdrop.addEventListener('mousedown', (e) => {
    if (e.target === backdrop) close(false);
  });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(backdrop);
  document.body.style.overflow = 'hidden';
  return { el: modal, close };
}

/**
 * await confirmDialog({ title, body, confirmText, tone }) → true / false
 * body 可以是字符串或 Node;tone 决定图标与确认按钮颜色。
 *
 * @param {{ title?: string, body?: string|Node, confirmText?: string, cancelText?: string, tone?: string }} [opts]
 * @returns {Promise<boolean>}
 */
export function confirmDialog({ title, body, confirmText = '确认', cancelText = '取消', tone = 'brand' } = {}) {
  return new Promise((resolve) => {
    const okCls = tone === 'danger' ? 'btn btn-danger' : tone === 'success' ? 'btn btn-success' : 'btn btn-primary';
    const ok = h('button', { class: okCls, type: 'button' }, confirmText);
    const cancel = h('button', { class: 'btn btn-ghost', type: 'button' }, cancelText);
    const content = h(
      'div',
      {},
      h(
        'div',
        { class: 'modal-head' },
        h('div', { class: `modal-icon tone-${tone}` }, icon(TONE_ICON[tone] || 'info', 'i-lg')),
        h('div', {}, h('div', { class: 'modal-title' }, title)),
      ),
      h('div', { class: 'modal-body' }, typeof body === 'string' ? h('p', {}, body) : body),
      h('div', { class: 'modal-foot' }, cancel, ok),
    );
    const { close } = openModal(content, { onClose: (r) => resolve(!!r), label: title });
    ok.addEventListener('click', () => close(true));
    cancel.addEventListener('click', () => close(false));
    setTimeout(() => ok.focus(), 30);
  });
}
