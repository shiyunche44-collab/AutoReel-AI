import React from "react";
import { registerRoot, Composition } from "remotion";
import { Layer } from "./Layer.jsx";

// 数据由 remotion_layer.py 写进 props(layer.json),时长/尺寸也由它决定
const Root = () => (
  <Composition
    id="Layer"
    component={Layer}
    width={1920}
    height={1080}
    fps={30}
    durationInFrames={30}
    defaultProps={{ cues: [], style: {} }}
    calculateMetadata={({ props }) => ({
      width: props.width || 1920,
      height: props.height || 1080,
      fps: props.fps || 30,
      durationInFrames: Math.max(1, Math.round((props.duration || 1) * (props.fps || 30))),
    })}
  />
);
registerRoot(Root);
