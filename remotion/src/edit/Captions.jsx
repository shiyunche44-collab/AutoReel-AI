// commercial 字幕:大号白字、细描边 + 柔和阴影、不带底框、不做关键词变色(对标头部说车号)。
// 字号 = 短边 × size(默认 0.065);横屏一条一行(Python 端按 16 字断好,时间由 char_t 卡)。
import React from "react";
import { useCurrentFrame, useVideoConfig } from "remotion";
import { FONT, clamp01, fitSize } from "./theme.js";

export const CleanCue = ({ cue, bottom = 0.075, size: rel = 0.065 }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const t = frame / fps - cue.start;
  const text = (cue.words || []).map((w) => w.text).join("") || cue.text || "";
  const fs = fitSize(text, Math.round(Math.min(width, height) * rel), width * 0.9);
  return (
    <div style={{
      position: "absolute", left: "5%", right: "5%", bottom: `${bottom * 100}%`, textAlign: "center",
      fontFamily: FONT, fontWeight: 900, fontSize: fs, lineHeight: 1.2, color: "#fff", letterSpacing: fs * 0.02,
      WebkitTextStroke: `${Math.max(2, Math.round(fs * 0.045))}px rgba(0,0,0,.85)`, paintOrder: "stroke fill",
      textShadow: `0 ${Math.round(fs * 0.05)}px ${Math.round(fs * 0.16)}px rgba(0,0,0,.6)`,
      opacity: clamp01(t / 0.07),
    }}>{text}</div>
  );
};
