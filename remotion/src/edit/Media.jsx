// 素材带里的一刀:运镜(推拉 / 平移 / 缓推)、转场、主体对准、嵌入字体。classic 与 commercial 共用。
import React, { useEffect, useState } from "react";
import {
  AbsoluteFill, Img, OffthreadVideo, interpolate, staticFile, useCurrentFrame,
  continueRender, delayRender,
} from "remotion";
import { TRANS, clamp01, ease, easeInOut, src } from "./theme.js";

// 竖屏版面网格(相对高度)。底部 ~20% 和右侧一列会被抖音的文案 / 按钮盖住,字和数据都不放那里
export const grid = (w, h) => {
  if (w >= h) return { band: { top: 0, height: h }, title: null, capBottom: 0.1 };
  return {
    title: { top: h * 0.05, height: h * 0.17 },
    band: { top: h * 0.235, height: h * 0.46 },
    capBottom: 0.2,
  };
};

// 嵌入字体:FontFace 加载完才放行出帧,避免前几帧用了回退字体
export const useEmbeddedFont = (file) => {
  const [handle] = useState(() => (file ? delayRender("edit-font") : null));
  useEffect(() => {
    if (!file) return;
    const face = new FontFace("EditFont", `url(${staticFile(file)})`);
    face.load().then((f) => { document.fonts.add(f); continueRender(handle); })
      .catch(() => continueRender(handle));
  }, [file, handle]);
};

// 运镜。classic:推 / 拉 / 平移,线性;commercial:视频 ≤3% 缓推(drift)、静图朝主体慢推(push_slow),缓动
export const motionTransform = (motion, p) => {
  switch (motion) {
    case "push": return `scale(${interpolate(p, [0, 1], [1.0, 1.12])})`;
    case "pull": return `scale(${interpolate(p, [0, 1], [1.12, 1.0])})`;
    case "pan_l": return `scale(1.16) translateX(${interpolate(p, [0, 1], [3.5, -3.5])}%)`;
    case "pan_r": return `scale(1.16) translateX(${interpolate(p, [0, 1], [-3.5, 3.5])}%)`;
    case "drift": return `scale(${1 + 0.03 * easeInOut(p)})`;
    case "push_slow": return `scale(${1.03 + 0.09 * easeInOut(p)})`;
    default: return "none";
  }
};

// ------------------------------------------------------------------ 转场
// commercial 的 whip:前一刀最后 WHIP/2 秒带水平动态模糊甩出去,后一刀前 WHIP/2 秒甩进来,
// 方向按换章次序交替;模糊用 SVG 的单向高斯(CSS blur 只能各向同性)
export const WHIP = 0.24;
const WHIP_SHIFT = 9; // %:位移不大,主要靠模糊;放大 1.2 盖住边
const ZOOM_IN = 0.26;

const transitionStyle = (kind, t) => {
  // classic。t:进入这一刀后的秒数
  if (t >= TRANS || kind === "cut" || kind === "flash") return {};
  const p = ease(Math.min(1, t / TRANS));
  if (kind === "whip") {
    return { transform: `translateX(${(1 - p) * 60}%)`, filter: `blur(${(1 - p) * 18}px)` };
  }
  if (kind === "zoom") {
    return { transform: `scale(${1.25 - 0.25 * p})`, opacity: p };
  }
  return {};
};

// commercial:返回 { style, blur };blur 是这一帧的水平模糊像素(0 = 不用滤镜)
const commercialMove = (clip, t, dur, width) => {
  const half = WHIP / 2;
  const maxBlur = width * 0.045;
  if (clip.transition === "whip" && t < half) {
    const k = 1 - easeInOut(clamp01(t / half)); // 1 → 0
    const dir = clip.whipDir || 1;
    return { style: { transform: `translateX(${dir * k * WHIP_SHIFT}%) scale(${1 + 0.2 * k})` },
      blur: maxBlur * k };
  }
  if (clip.exit === "whip" && dur - t < half) {
    const k = easeInOut(clamp01(1 - (dur - t) / half)); // 0 → 1
    const dir = clip.exitDir || 1;
    return { style: { transform: `translateX(${-dir * k * WHIP_SHIFT}%) scale(${1 + 0.2 * k})` },
      blur: maxBlur * k };
  }
  if (clip.transition === "zoom" && t < ZOOM_IN) {
    const k = 1 - ease(clamp01(t / ZOOM_IN));
    return { style: { transform: `scale(${1 + 0.14 * k})`, filter: `blur(${(k * 10).toFixed(1)}px)` },
      blur: 0 };
  }
  return { style: {}, blur: 0 };
};

// 主体在原片的水平位置 fx → object-position 百分比:让主体尽量落在框的正中。
// cover 时原片按高度撑满,横向多出 (sw - bw);窗口左边 = p × 多出的部分
export const objectX = (fx, srcAR, box) => {
  if (fx == null || !box) return 0.5;
  const sw = box.h * (srcAR || 16 / 9);
  if (sw <= box.w + 1) return 0.5;
  return Math.min(1, Math.max(0, (fx * sw - box.w / 2) / (sw - box.w)));
};

export const Media = ({ clip, fps, cover = true, box = null }) => {
  const frame = useCurrentFrame();
  const total = Math.max(1, Math.round(clip.dur * fps));
  const p = Math.min(1, frame / total);
  // 去角标 / 去烧录字幕:四边收 crop_edge,底部再裁 crop_bottom
  const crop = 1 / Math.max(0.5, 1 - 2 * (clip.crop_edge || 0) - (clip.crop_bottom || 0));
  const shiftY = (clip.crop_bottom || 0) * 50;
  // 自动重构图:裁切窗口和推拉中心都对准主体(focus 由 Python 按边缘能量算出,缺省居中)
  const fx = `${(objectX(clip.focus, clip.ar, box) * 100).toFixed(1)}%`;
  const style = {
    width: "100%", height: "100%", objectFit: cover ? "cover" : "contain",
    objectPosition: `${fx} 50%`, transformOrigin: `${fx} 50%`,
    transform: `${motionTransform(clip.motion, p)} scale(${crop}) translateY(-${shiftY}%)`,
  };
  if (clip.kind === "still") return <Img src={src(clip.src)} style={style} />;
  return (
    <OffthreadVideo src={src(clip.src)} muted startFrom={Math.round((clip.src_in || 0) * fps)}
      style={style} />
  );
};

// classic:首刀白闪,换章 whip 带一点白;commercial 只有首刀白闪
const Flash = ({ clip, fps, commercial }) => {
  const frame = useCurrentFrame();
  if (clip.transition !== "flash" && (commercial || clip.transition !== "whip")) return null;
  const peak = clip.transition === "flash" ? 1 : 0.35;
  const o = interpolate(frame / fps, [0, 0.22], [peak, 0], { extrapolateRight: "clamp" });
  return <AbsoluteFill style={{ background: "#fff", opacity: o }} />;
};

export const BandClip = ({ clip, fps, box = null, commercial = false, width = 1920 }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  if (!commercial) {
    return (
      <AbsoluteFill style={{ overflow: "hidden" }}>
        <AbsoluteFill style={transitionStyle(clip.transition, t)}>
          <Media clip={clip} fps={fps} box={box} />
        </AbsoluteFill>
        <Flash clip={clip} fps={fps} />
      </AbsoluteFill>
    );
  }
  const mv = commercialMove(clip, t, clip.dur, width);
  const fid = `whip-${Math.round(clip.start * 1000)}`;
  const blur = mv.blur > 0.5;
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {blur && (
        <svg width="0" height="0" style={{ position: "absolute" }}>
          <filter id={fid} x="-10%" y="0" width="120%" height="100%">
            <feGaussianBlur stdDeviation={`${mv.blur.toFixed(1)} 0`} />
          </filter>
        </svg>
      )}
      <AbsoluteFill style={{ ...mv.style, ...(blur ? { filter: `url(#${fid})` } : {}) }}>
        <Media clip={clip} fps={fps} box={box} />
      </AbsoluteFill>
      <Flash clip={clip} fps={fps} commercial />
    </AbsoluteFill>
  );
};
