# 概念动画风格小样(第一阶段)

同一份内容(电池与续航 / 零百加速 / 外观),做三种风格,各 15 秒,用来挑第二阶段的方向。数字都是示例数据,不是核对过的事实。

| 文件 | 风格 | 用到的东西 |
|---|---|---|
| `A.html` | 玻璃拟态 | 实拍视频做底 + `backdrop-filter` 毛玻璃卡、光扫、`shared/gallery3d.js` 3D 展厅画廊;转场 whip-pan / light-leak |
| `B.html` | 电影暗调 3D | 宽银幕遮幅、颗粒、暗角;`shared/battery3d.js` 电池分层爆炸图;`vendor/motion-blur.js` 动态模糊砸字;推轨 + 移焦;转场 cinematic-zoom / flash-through-white |
| `C.html` | 极简科技 HUD(浅色) | 发丝线网格、等宽数字、取景框 + 跟踪框、速度标尺与秒表同步(真实 3.23 秒)、官图尺寸线绘制;转场 chromatic-split / glitch |

## 出片

```bash
cd motion
node node_modules/hyperframes/bin/hyperframes.mjs render bakeoff -c A.html -o bakeoff/out/A.mp4 --fps 24 --workers 4
```

需要的本地准备(都在 .gitignore 里,不进仓库):
- `assets/studio.mp4`、`assets/road.mp4`:从素材库 `小米yu7/raw/b57660ca1c_0.mp4`(小米 YU7 官方发布片)37.6s、73.9s 起截取,裁掉遮幅和底部免责声明字幕。
- `assets/stills/yu7_*.jpg`:素材库里的汽车之家官图,缩到 1600 宽。
- `fonts/NotoSansSC-VF.ttf`:拷自 `C:\Windows\Fonts`(SIL OFL)。

## 来源与许可
- `vendor/shader-transitions.global.js`:`@hyperframes/shader-transitions@0.8.138`(Apache-2.0)。
- `vendor/three.r147.min.js`:three.js r147 全局版(MIT),画廊和电池用。
- `vendor/motion-blur.js`:取自 HyperFrames registry 的 `motion-blur` 组件(Apache-2.0)。
- `gallery3d.js`、`battery3d.js` 是参照 registry 的 `carousel-vision-3` / `exploded-stack` 的做法自写的。
- `../registry/` 是 `hyperframes add` 拉下来的原始组件(Apache-2.0),只作参照。`carousel-vision-3` 自带的第三方专辑封面图已经删掉。
