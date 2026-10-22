import { afterEach, describe, expect, it, vi } from 'vitest';
import { confirmDialog, openModal } from '../../app/web/static/js/ui/modal.js';

afterEach(() => {
  document.body.innerHTML = '';
  document.body.style.overflow = '';
  vi.useRealTimers();
});

describe('openModal()', () => {
  it('挂出弹窗并锁住页面滚动', () => {
    const { el } = openModal(document.createTextNode('内容'), { label: '测试' });
    expect(el.getAttribute('role')).toBe('dialog');
    expect(el.getAttribute('aria-label')).toBe('测试');
    expect(document.body.style.overflow).toBe('hidden');
  });

  it('按 Esc 关闭并回调 onClose', () => {
    const onClose = vi.fn();
    openModal(document.createTextNode('内容'), { onClose });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.body.style.overflow).toBe('');
  });

  it('点遮罩空白处关闭', () => {
    const onClose = vi.fn();
    openModal(document.createTextNode('内容'), { onClose });
    document.querySelector('.modal-backdrop').dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('只关一次', () => {
    const onClose = vi.fn();
    const { close } = openModal(document.createTextNode('内容'), { onClose });
    close();
    close();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('confirmDialog()', () => {
  it('点确认返回 true', async () => {
    const p = confirmDialog({ title: '确认?', body: '这一步不能撤' });
    const [cancel, ok] = document.querySelectorAll('.modal-foot button');
    expect(cancel.textContent).toBe('取消');
    expect(ok.textContent).toBe('确认');
    ok.click();
    await expect(p).resolves.toBe(true);
  });

  it('点取消返回 false', async () => {
    const p = confirmDialog({ title: '确认?', body: '这一步不能撤' });
    document.querySelectorAll('.modal-foot button')[0].click();
    await expect(p).resolves.toBe(false);
  });

  it('按 Esc 等于取消', async () => {
    const p = confirmDialog({ title: '确认?', body: '这一步不能撤' });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await expect(p).resolves.toBe(false);
  });

  it('语气决定确认按钮的样式', () => {
    confirmDialog({ title: '删掉?', body: '删了就没了', tone: 'danger' });
    const ok = document.querySelectorAll('.modal-foot button')[1];
    expect(ok.className).toContain('btn-danger');
  });

  it('正文可以是节点', async () => {
    const p = confirmDialog({ title: '确认?', body: document.createTextNode('一段说明') });
    expect(document.querySelector('.modal-body').textContent).toContain('一段说明');
    document.querySelectorAll('.modal-foot button')[1].click();
    await expect(p).resolves.toBe(true);
  });
});
