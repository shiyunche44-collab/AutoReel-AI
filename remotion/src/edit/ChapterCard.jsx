// commercial 章节卡:换章时约 1 秒,实拍模糊压暗,中间偏左出章节号和章名,强调色细线拉开。
// 替代 classic 的章节小标和底部进度条。
import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { FONT, clamp01, ease, fitSize, inOut, strokeText } from "./theme.js";

export const ChapterCard = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  // 换章时素材带先 whip 甩进来,Edit.jsx 已把这一层的起点挪到 whip 落定之后
  const t = frame / fps;
  const k = inOut(t, o.dur, 0.22, 0.2);
  const size = Math.round(band.height * (band.land ? 0.1 : 0.075));
  const name = String(o.props.name || "");
  const fs = fitSize(name, size, band.w * 0.8, 0.3);
  const n = String(o.props.n || 1).padStart(2, "0");
  const total = o.props.total ? ` / ${String(o.props.total).padStart(2, "0")}` : "";
  const line = ease(clamp01((t - 0.08) / 0.4));
  const small = Math.round(fs * 0.3);
  return (
    <AbsoluteFill style={{ backdropFilter: `blur(${(k * 18).toFixed(1)}px)`,
      background: `rgba(0,0,0,${(k * 0.42).toFixed(3)})` }}>
      <div style={{ position: "absolute", left: "8%", right: "8%", top: 0, bottom: band.land ? "14%" : 0,
        display: "flex", flexDirection: "column", justifyContent: "center", opacity: k }}>
        <div style={{ fontFamily: FONT, fontWeight: 900, fontSize: small, color: accent, letterSpacing: small * 0.12,
          fontVariantNumeric: "tabular-nums", transform: `translateY(${(1 - k) * 16}px)` }}>
          {n}<span style={{ color: "rgba(255,255,255,.55)" }}>{total}</span>
        </div>
        <div style={{ width: line * fs * 2.4, height: Math.max(3, small * 0.14),
          background: accent, margin: `${small * 0.45}px 0 ${small * 0.5}px` }} />
        <div style={{ fontFamily: FONT, fontWeight: 900, fontSize: fs, color: "#fff", lineHeight: 1.15,
          whiteSpace: "nowrap", transform: `translateY(${(1 - k) * 28}px) skewX(-9deg)`, transformOrigin: "0 100%",
          ...strokeText(fs, 0.04) }}>{name}</div>
      </div>
    </AbsoluteFill>
  );
};
