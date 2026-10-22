/* 渲染看板里的视频:懒加载 + 单拍地址失效时退回草稿。 */

/**
 * 给视频挂上回退:成片合成后 shots/ 里的单拍会被清掉,只剩草稿。
 * 视频地址报错时换成草稿地址。
 * @param {HTMLVideoElement} v
 * @returns {HTMLVideoElement} 同一个 v
 */
export function withFallback(v) {
  v.addEventListener('error', () => {
    const fb = v.dataset.fallback;
    if (!fb || v.dataset.src === fb) return;
    v.dataset.src = fb;
    const wasPlaying = v.closest('.shot.is-playing');
    v.src = v.src.includes('#t=') ? `${fb}#t=0.1` : fb;
    if (wasPlaying || v.autoplay) v.play().catch(() => {});
  });
  return v;
}

/**
 * 真正加载视频。#t=0.1 让浏览器解出第一帧当封面,不然是黑块。
 * @param {HTMLVideoElement} v
 */
export function loadVideo(v) {
  if (v.src) return;
  withFallback(v);
  v.preload = 'metadata';
  v.src = `${v.dataset.src}#t=0.1`;
}
