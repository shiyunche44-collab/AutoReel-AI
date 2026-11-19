---
name: storyboard
description: AutoReel 分镜。负责逐拍定版式（broll/panel/highlight/hook/card/stat/concept）、填叠字数据，以及门禁 A 改分镜。适用于 step 停在 purpose 为 pipeline.explainer.* 的请求，或者门禁 A 要改分镜的时候。
---

# storyboard：分镜

## 回答分镜请求（`pipeline.explainer.build_beats`）
一段稿子对应一拍。你要定每拍的版式，并填上叠加层的数据。提示词里列了每拍的"可选版式"和"可用事实"，选择范围就在这些里面。
- **叠字上的数字只能来自"可用事实"**，`fact_id` 要填，`values` 要和事实原文逐字一致，单位也一样。核对不过的拍会**被静默降成 broll**，所以写错的代价是这一拍白做。
- **对比卡**：每行只引用一条事实，而且这条事实里要同时写着两台车的取值。找不到这样的事实，就别做对比卡。
- **节奏**：
  - broll 不超过三成。
  - 不要连续两拍 stat 或 concept，card 不要连续 3 拍以上。
  - 旁白里讲到了数字，这一拍就把数字放上屏（panel、stat、concept 都可以）。
- **concept**：只用在讲看不见的东西时，比如电池、补能、加速、感知、芯片、尺寸。全片 3–6 拍，`scene` 只能从提示词列出的模版里选。（P2 之后可以由你现写场景，见 motion skill。）
- **keywords**：每行 12 字以内，里面的型号和数字必须在这一拍的旁白或事实里出现过。
- `view` 决定从素材池取外观、内饰还是行驶画面：讲座舱给 interior，讲尺寸和驾驶给 exterior 或 driving。
- 交之前在脑子里过一遍：从头到尾看下来，版式有没有穿插开？数字有没有都上屏？

## 关键词拍（`pipeline.explainer.planning_b`）
请求要的是 `items`：给指定的几拍挑最该上屏的词。规则同上，词必须在旁白或事实里出现过。

## 门禁 A 改分镜
```bash
python -m app.cli shots <pid>                    # 现在的分镜(ShotSpec 数组)
python -m app.cli shots-put <pid> shots.json     # 整组写回
```
写回时会按同一套规则核对。改分镜前先把稿子改定，分镜依附在稿子上。
