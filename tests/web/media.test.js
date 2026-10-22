import { describe, expect, it, vi } from 'vitest';
import { loadVideo, withFallback } from '../../app/web/static/js/ui/media.js';

/** 造一个渲染看板里的视频元素。 */
function makeVideo({ src = '', fallback = '', autoplay = false } = {}) {
  const v = document.createElement('video');
  if (src) v.setAttribute('data-src', src);
  if (fallback) v.setAttribute('data-fallback', fallback);
  if (autoplay) v.setAttribute('autoplay', '');
  // jsdom 不实现 play(),自己顶一个
  v.play = vi.fn(() => Promise.resolve());
  return v;
}

describe('withFallback()', () => {
  it('返回的还是同一个视频', () => {
    const v = makeVideo({ src: '/media/a.mp4', fallback: '/media/a-draft.mp4' });
    expect(withFallback(v)).toBe(v);
  });

  it('单拍地址报错时换成草稿地址', () => {
    const v = makeVideo({ src: '/media/a.mp4', fallback: '/media/a-draft.mp4' });
    withFallback(v);
    v.dispatchEvent(new Event('error'));
    expect(v.dataset.src).toBe('/media/a-draft.mp4');
    expect(v.src).toContain('/media/a-draft.mp4');
  });

  it('已经在草稿地址上就不换(否则会来回死循环)', () => {
    const v = makeVideo({ src: '/media/a-draft.mp4', fallback: '/media/a-draft.mp4' });
    withFallback(v);
    v.dispatchEvent(new Event('error'));
    expect(v.dataset.src).toBe('/media/a-draft.mp4');
  });

  it('没有草稿地址就不动', () => {
    const v = makeVideo({ src: '/media/a.mp4' });
    withFallback(v);
    v.dispatchEvent(new Event('error'));
    expect(v.dataset.src).toBe('/media/a.mp4');
  });
});

describe('loadVideo()', () => {
  it('带 #t=0.1 让浏览器解出第一帧当封面', () => {
    const v = makeVideo({ src: '/media/a.mp4' });
    loadVideo(v);
    expect(v.src).toContain('/media/a.mp4');
    expect(v.src).toContain('#t=0.1');
    expect(v.preload).toBe('metadata');
  });

  it('同时挂上 404 回退', () => {
    const v = makeVideo({ src: '/media/a.mp4', fallback: '/media/a-draft.mp4' });
    loadVideo(v);
    v.dispatchEvent(new Event('error'));
    expect(v.dataset.src).toBe('/media/a-draft.mp4');
  });

  it('已经加载过就不重复加载', () => {
    const v = makeVideo({ src: '/media/a.mp4' });
    loadVideo(v);
    const first = v.src;
    loadVideo(v);
    expect(v.src).toBe(first);
  });
});
