import React from "react";
import { registerRoot, Composition } from "remotion";
import { Layer } from "./Layer.jsx";
import { Edit } from "./Edit.jsx";

// 数据由 Python 写进 props:Layer 来自 caption_layer.py,Edit 来自 pipeline/edit/render.py。
// 时长 / 尺寸都由 props 决定
const meta = ({ props }) => ({
  width: props.width || 1920,
  height: props.height || 1080,
  fps: props.fps || 30,
  durationInFrames: Math.max(1, Math.round((props.duration || 1) * (props.fps || 30))),
});

const Root = () => (
  <>
    <Composition
      id="Layer"
      component={Layer}
      width={1920}
      height={1080}
      fps={30}
      durationInFrames={30}
      defaultProps={{ cues: [], style: {} }}
      calculateMetadata={meta}
    />
    <Composition
      id="Edit"
      component={Edit}
      width={1080}
      height={1920}
      fps={30}
      durationInFrames={30}
      defaultProps={{ clips: [], overlays: [], captions: [], chapters: [], title: "", duration: 1 }}
      calculateMetadata={meta}
    />
  </>
);
registerRoot(Root);
