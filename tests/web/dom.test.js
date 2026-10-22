import { afterEach, describe, expect, it, vi } from 'vitest';
import { $, $$, debounce, h, icon, readJSON, relTime, withLoading } from '../../app/web/static/js/ui/dom.js';

afterEach(() => {
  document.body.innerHTML = '';
  vi.useRealTimers();
});

describe('h()', () => {
  it('建元素并挂 class', () => {
    const el = h('div', { class: 'x y' });
    expect(el.tagName).toBe('DIV');
    expect(el.className).toBe('x y');
  });

  it('字符串一律当文本,不解析成 HTML', () => {
    const el = h('div', {}, '<b>hi</b>');
    expect(el.textContent).toBe('<b>hi</b>');
    expect(el.querySelector('b')).toBeNull();
  });

  it('null / false / undefined 的子节点跳过', () => {
    const el = h('div', {}, null, false, undefined, 'a');
    expect(el.childNodes.length).toBe(1);
    expect(el.textContent).toBe('a');
  });

  it('数组子节点摊平', () => {
    const el = h('div', {}, [h('i', {}, 'a'), h('b', {}, 'b')]);
    expect(el.children.length).toBe(2);
    expect(el.textContent).toBe('ab');
  });

  it('数字子节点转成文本', () => {
    expect(h('div', {}, 42).textContent).toBe('42');
  });

  it('on* 开头且是函数的属性挂成事件监听', () => {
    const fn = vi.fn();
    h('button', { onclick: fn }).click();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('true 的属性写成空字符串,null / false 的不写', () => {
    const el = h('input', { disabled: true, value: null, checked: false });
    expect(el.getAttribute('disabled')).toBe('');
    expect(el.hasAttribute('value')).toBe(false);
    expect(el.hasAttribute('checked')).toBe(false);
  });
});

describe('$ / $$', () => {
  it('查元素与查一组', () => {
    document.body.innerHTML = '<div id="a"><span class="s"></span><span class="s"></span></div>';
    expect($('#a')).not.toBeNull();
    expect($$('.s').length).toBe(2);
  });

  it('查不到时 $ 返回 null', () => {
    expect($('#nope')).toBeNull();
  });
});

describe('readJSON()', () => {
  it('读模板注入的 JSON', () => {
    document.body.innerHTML = '<script type="application/json" id="d">{"a":1}</script>';
    expect(readJSON('d')).toEqual({ a: 1 });
  });

  it('元素不存在时用兜底值', () => {
    expect(readJSON('nope', { b: 2 })).toEqual({ b: 2 });
  });

  it('JSON 坏了也用兜底值,不抛', () => {
    document.body.innerHTML = '<script type="application/json" id="d">{oops</script>';
    expect(readJSON('d', 'fb')).toBe('fb');
  });
});

describe('relTime()', () => {
  it('不到一分钟就是"刚刚"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-22T12:00:00Z'));
    expect(relTime(Date.now() / 1000 - 10)).toBe('刚刚');
  });

  it('分钟 / 小时 / 天', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-22T12:00:00Z'));
    const now = Date.now() / 1000;
    expect(relTime(now - 120)).toBe('2 分钟前');
    expect(relTime(now - 7200)).toBe('2 小时前');
    expect(relTime(now - 86400 * 3)).toBe('3 天前');
  });

  it('超过一周给日期', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-22T12:00:00Z'));
    const old = new Date('2026-10-01T12:00:00Z').getTime() / 1000;
    expect(relTime(old)).toBe('10 月 1 日');
  });

  it('时间戳坏了返回空串', () => {
    expect(relTime('abc')).toBe('');
  });
});

describe('withLoading()', () => {
  it('跑的时候按钮禁用并挂 is-loading,跑完恢复', async () => {
    const btn = h('button', {});
    let seen = null;
    await withLoading(btn, async () => {
      seen = btn.classList.contains('is-loading');
      expect(btn.disabled).toBe(true);
    });
    expect(seen).toBe(true);
    expect(btn.classList.contains('is-loading')).toBe(false);
    expect(btn.disabled).toBe(false);
  });

  it('把回调的返回值带出来', async () => {
    await expect(withLoading(h('button', {}), async () => 'ok')).resolves.toBe('ok');
  });

  it('回调抛错也要恢复按钮状态', async () => {
    const btn = h('button', {});
    await expect(
      withLoading(btn, async () => {
        throw new Error('boom');
      }),
    ).rejects.toThrow('boom');
    expect(btn.disabled).toBe(false);
    expect(btn.classList.contains('is-loading')).toBe(false);
  });

  it('已经在加载中就不再跑一次(防重复提交)', async () => {
    const btn = h('button', { class: 'is-loading' });
    const fn = vi.fn();
    await withLoading(btn, fn);
    expect(fn).not.toHaveBeenCalled();
  });
});

describe('debounce()', () => {
  it('连点只跑最后一次', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const debounced = debounce(fn, 100);
    debounced('a');
    debounced('b');
    debounced('c');
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(100);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith('c');
  });
});

describe('icon()', () => {
  it('生成引用雪碧图的 svg', () => {
    const el = icon('check', 'i-sm');
    expect(el.tagName.toLowerCase()).toBe('svg');
    expect(el.getAttribute('aria-hidden')).toBe('true');
    expect(el.getAttribute('class')).toBe('i i-sm');
    expect(el.querySelector('use').getAttribute('href')).toBe('#i-check');
  });
});
