---
name: script
description: AutoReel 写稿。负责论证大纲、开场钩子、全文、压字数、口语二改、自修，以及门禁 A 前的自审和改稿。适用于 step 停在 purpose 为 pipeline.script_gen.* 的请求，或者门禁 A 要改稿的时候。
---

# script：写稿

一份稿子会连着出好几个请求，按出现的顺序逐个回答：

| purpose（模块） | 要你做什么 | 要点 |
|---|---|---|
| `script_gen.brief` | 写前分析：核心矛盾、要论证什么 | 想清楚再写。这份分析会当作大纲交给下一步，也会存进 `review_brief.json` |
| `script_gen.choose_hook` 的候选 | 写 n 版开场第一句 | 每版都在字数线以内、带判断、`fact_refs` 填对。第二轮请求说"太长了"，就把铺垫删干净 |
| `script_gen.choose_hook` 的打分 | 给候选打分 | 按"有没有人会反对、3 秒内听不听得懂"打分，**不要偏袒自己写的那版**，照实打 |
| `script_gen.generate` | 写全文 | 见下 |
| `script_gen.fit` | 压到目标字数 | 先删重复和铺垫，再删次要参数；主论点、钩子、结论不能删 |
| `script_gen.spoken_pass` | 口语二改 | 只换说法，**不能加数字**，口径和"谁说的"一个字都不能丢 |
| `script_gen.repair` | 修 lint 报的段 | 有出处的数字补上引用，没出处的改成定性说法 |

## 写全文时
提示词里的规则就是写作规范，逐条照办。最容易出错的几处：
- **数字**：每个数字都要在同段的 `fact_refs` 里有对应的事实，数值和单位逐字一致，口径也要说出来（"CLTC 标称"）。derived 来源的数字要加"约""粗算"。
- **段落数**：必须和"段落序列"一一对应，`section` 照抄。
- **主论点 `thesis`**：30 字以内，是个判断。最后一段要**原样包含**这句话。
- **开场**：第一句 18 字以内，是立场而不是选题。开头三段各做一件事：冲突、兑现、说明出处。
- **结尾**：给出行动，提一个问题或预告下期。
- **去 AI 味**："不是 X，是 Y""代价是"这类句式全片不超过 3 次。同一个结论只在开场和结尾各说一次。每段写法要有变化。
- 不要写"我没有数据""资料显示"这种话。没依据的内容整段不写。
- 写完通读一遍，找重复的结论和重复的句式。可以对照 humanizer-zh skill 的原则自查，但不要动数字和事实引用。

## 门禁 A 改稿
```bash
python -m app.cli script <pid>               # 稿子 + lint
python -m app.cli lint <pid>                 # 放行拦截项
python -m app.cli script-put <pid> s.json    # 整稿写回,返回新 lint
```
`script-put` 要交完整的 `{"segments":[...],"title":"","thesis":""}`，segments 的结构和请求里的一样。lint 里有 error 必须修干净，warning 逐条看过后再决定改不改。改完稿子后，分镜里的叠字可能会对不上，用 `shots` 看一眼。
