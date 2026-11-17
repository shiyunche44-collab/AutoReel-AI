# 剪辑 v2:统一时间线 + Remotion 主合成

状态:**第 1–3 阶段已落地(试点,默认关闭)**。`EDIT_V2=1` 只对竖屏生效,`EDIT_V2=all` 横竖都走;
任何失败自动退回旧合成,并记一条 `edit_v2:fallback` 事件。

## 为什么重构

审片日志(`docs/team-loop-log.md`)和后台数据里反复出现的问题,与旧方案的结构一一对应:

| 现象 | 旧方案的结构原因 |
|---|---|
| 画面与旁白不符(讲双联屏配车侧面)、串车 | `library.fill` 设计上"背景不要求对上旁白",一拍共用一组背景 |
| 像 PPT、节奏平 | 一拍一版式且整拍烧死;切点不跟着词;拍间基本硬切 |
| 包装土 | 叠加层是 PIL 出的 PNG + ffmpeg overlay,动效只能做很有限的关键帧 |
| 2 秒跳出 30–50% | 开场只是一种版式,没有专门的钩子编排 |
| 难微调 | 没有统一时间线,改一处要重渲整拍 |

## 参考的成熟方案

- **剪映「图文成片」/ CapCut、Opus Clip、Pictory**:按句配画面、关键词花字、1.5–3 秒一刀、转场带音效 —— 目标观感。
- **NarratoAI**(GitHub):视觉模型先看懂素材,LLM 按解说逐句挑片段 —— 第 2 阶段"语义配画面"的思路。
- **MoneyPrinterTurbo**:按句关键词检索素材 + MoviePy 合成 —— 借"每句单独取材",不借合成器。
- **Remotion / Revideo**:React 声明式时间线、逐帧可控 —— 选作主合成器(仓库已在用 Remotion 做花字层)。
- **OpenTimelineIO / editly**:多轨 JSON 描述时间线 —— `edl.py` 的数据形状参照它们。
- **pyJianYingDraft**:生成剪映草稿供人精修 —— 可作为第 4 阶段的人工兜底出口。
- **Chinese-CLIP 等多模态向量 + PySceneDetect**:句子 → 镜头的语义检索 —— 第 2 阶段。

## 架构

```
分镜(ShotSpec, TTS 定好时长) + narration.json(cue + char_t 字级时间)
        │
        ▼
pipeline/edit/edl.py      build_timeline():纯函数
  - 实拍拍在分句处下刀(MIN_SHOT 1.2s – MAX_SHOT 2.8s)
  - 每刀把"这一刀念到的半句"交给 pick() 取材 → library.fill(detail=半句)
  - 开头 3 秒钩子:≤1s 一刀 + 大标题,首刀白闪
  - 转场:同拍硬切 / 换章 whip / 进出整屏画面 zoom
  - card / concept / mech 用逐拍成品整段放;取不到素材退回逐拍成品对应片段
  - 叠加层:hook / keyword / stat / panel / chapter;字幕复用 caption_layer
        │  timeline.json(edit_timeline.json 落盘便于复盘)
        ▼
remotion/src/Edit.jsx     竖屏:标题带 + 章节进度 / 素材带(推拉 + 转场 + 叠加层)/ 花字字幕
        │  静音 h264
        ▼
pipeline/edit/render.py   mix.wav(沿用 render._mix_audio,音效在 whip/zoom/flash 处 whoosh)
                          → -c:v copy 合流 → 解码检查 + 音频包检查 + final_qa
```

流水线状态机不变:DRAFT_RENDERING 照旧逐拍渲染(门禁 B 审片、整屏卡片 / 概念动画都要用),
只有 COMPOSING 里的合成换成 v2。封面、包装、render.json 新旧共用(`explainer._finish_compose`)。

## 分阶段

1. **(已完成)时间线 + Remotion 合成 + 按半句取材 + 钩子 + 转场**。`tests/test_edit_edl.py`。
2. **(已完成)语义配画面**(`pipeline/edit/match.py`,`tests/test_edit_match.py`):
   - 视角意图:词表 `vocab.view_words`(说法 → 素材视角),每刀按半句推出想要的视角,
     `library.fill(views=...)` 把命中视角的素材当"对题"排前;
   - 文字模型复选(`EDIT_V2_RERANK`,默认开):`fill` 排序前 6 名的素材描述 + 这半句交给文字模型挑一条
     (`fill(choose=...)`);模型出错 / 编号不合法就按排序取;
   - 对题率:半句点到了视角 / 部件的刀里,素材视角或描述对上的比例,写进 `edit_timeline.json` 的
     `match`,也作为 render.json 的第一条提示(「画面对题 x/y 刀」)。拿它和 cut_review 的
     "画面与旁白不符"一起看趋势;
   - 只改字幕重出成片(`explainer.recompose`)时沿用上次的时间线,同样的取材和刀口,只换字幕。
   - 向量召回(方舟多模态 embedding / Chinese-CLIP)暂缓:Agent Plan 的 Key 是否含向量接口未验证,
     且现有视觉标签 + 文字模型复选已覆盖主要场景。对题率长期上不去再加。
3. **(已完成大部分)包装与节奏**:
   - 数据卡(spec / compare / bars / timeline)改成 Remotion 组件:卡片拍照常取材铺素材带,
     上面压一张玻璃卡,行按旁白时刻(`at`)出现,对比卡左右先后亮,柱状图生长;
     不认识的卡片种类仍用逐拍成品。逐拍渲染仍会出旧卡片(门禁 B 审片用),成片不再用它;
   - 关键词冲击推(`Timeline.punches`):花字关键词念出时素材带放大 7% 再回落,
     至少隔 2.5 秒,钩子段 / 整屏画面 / 数据层期间不推;
   - 视觉包主题(`project.theme_of`):强调色、第二强调色、字幕样式(box 黑底条 / band 色块 /
     outline 描边)跟着 look 走,深色强调色自动提亮到深底上看得清;
   - 未做:概念动画 / 机构画面迁 Remotion(HyperFrames 模版多、回归基线在本机,单独排期);
     钩子 A/B(要等试点有了对题率和 2 秒跳出数据再设计)。
4. **人工精修出口(可选)**:由 timeline.json 导出剪映草稿。
5. 竖屏数据稳定后,横屏切换并删除旧合成路径。

## 试点怎么验证

**先试已经出过片的项目(推荐)**:不改项目状态、不覆盖原成片,只把合成换成 v2,出一份对比图。

```bash
python -I scripts/edit_v2_trial.py --list          # 已完成的竖屏项目
python -I scripts/edit_v2_trial.py                 # 自动挑最近一条
python -I scripts/edit_v2_trial.py p_xxx --no-rerank
```

产物在项目目录:`output_v2_trial.mp4`(新片)、`_edit/compare.jpg`(上排旧片、下排新片)、
`edit_timeline.json`(每一刀念到的半句、取的素材、对没对上题)。

**再在流水线里开**:

1. 本机 `remotion/` 下 `npm ci`;`.env` 加 `EDIT_V2=1`;用 `scripts/restart_server.ps1 -Reason "开剪辑v2试点"` 重启。
2. 找一个竖屏项目在门禁 B 通过,等 COMPOSING 自动出片;`render.json` 的 warnings 第一条是「剪辑 v2 合成」。
3. 失败时看项目事件 `edit_v2:fallback`(已退回旧成片)。`edit_timeline.json` 里每刀的 `text` 就是它取材用的半句。
4. 渲染时长:Remotion 比 ffmpeg 慢。4 核云容器实测 20 秒成片约 70 秒(竖屏模糊底图改成预先抽帧
   模糊的小图后,比实时 CSS 模糊快约 2.4 倍),本机核数多会更快。`EDIT_V2_CONCURRENCY`(默认 `50%`)控制并发,`EDIT_V2_TIMEOUT` 默认 3600 秒。

## 已知限制(第 1 阶段)

- 主持人层、片头角标(intro)v2 里暂不叠。
- 对题依赖入库时视觉模型写的描述和视角标签;标错了的素材照样会配错。
- 竖屏的概念动画 / 机构画面仍是逐拍成品,自带旧样式标题带,与 v2 标题带略有差异。
