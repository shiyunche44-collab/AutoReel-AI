import { afterEach, describe, expect, it, vi } from 'vitest';
import { toast } from '../../app/web/static/js/ui/toast.js';

afterEach(() => {
  document.body.innerHTML = '';
  vi.useRealTimers();
});

function stack() {
  return document.querySelector('.toast-stack');
}

describe('toast()', () => {
  it('弹一条到提示栈里', () => {
    toast('已保存');
    expect(stack().querySelectorAll('.toast').length).toBe(1);
    expect(stack().textContent).toContain('已保存');
  });

  it('type 决定配色', () => {
    toast('炸了', { type: 'danger' });
    expect(stack().querySelector('.toast').classList.contains('tone-danger')).toBe(true);
  });

  it('标题单独一行', () => {
    toast('正文', { title: '保存失败' });
    expect(stack().querySelector('.toast-title').textContent).toBe('保存失败');
  });

  it('最多留 4 条,多的把最旧的挤掉', () => {
    for (let i = 1; i <= 6; i++) toast(`第 ${i} 条`);
    const list = stack().querySelectorAll('.toast');
    expect(list.length).toBe(4);
    expect(list[0].textContent).toContain('第 3 条');
  });

  it('到时间自动退场', () => {
    vi.useFakeTimers();
    toast('会消失', { duration: 1000 });
    expect(stack().querySelector('.toast').classList.contains('is-leaving')).toBe(false);
    vi.advanceTimersByTime(1000);
    expect(stack().querySelector('.toast').classList.contains('is-leaving')).toBe(true);
  });

  it('duration 为 0 就常驻', () => {
    vi.useFakeTimers();
    toast('常驻', { duration: 0 });
    vi.advanceTimersByTime(60000);
    expect(stack().querySelector('.toast').classList.contains('is-leaving')).toBe(false);
  });

  it('带动作按钮时点它会执行并关掉提示', () => {
    const onClick = vi.fn();
    toast('要重试吗', { action: { label: '重试', onClick } });
    const btn = stack().querySelector('.toast-body button');
    expect(btn.textContent).toBe('重试');
    btn.click();
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(stack().querySelector('.toast').classList.contains('is-leaving')).toBe(true);
  });
});
