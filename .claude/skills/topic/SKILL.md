---
name: topic
description: 为 AutoReel 选题。抓实时热点，由 Claude 提出车型、角度和钩子，再用现有规则过滤，最后交用户拍板。用户说"找选题 / 最近做什么 / 有什么热点"，或 make-video 开工前还没定车型时使用。
---

# topic：热点选题

```bash
python -m app.cli topics fetch [--n 8] [--category auto_long]   # 抓懂车帝榜单 / 新闻 / 抖音 / B站热点
python -m app.cli topics prompt                                  # 选题提示词原文(规则 + 热点列表)
python -m app.cli topics put topics.json                         # 交选题,按规则过滤
```

1. 先 `fetch`。`errors` 里列出的来源这次没抓到，B站被风控是常事，不要连续重抓。
2. `prompt` 里的规则照办。回答格式是 `{"topics":[...]}`，字段说明见提示词。
3. `put` 会丢掉不合规的选题：车型没在热点里出现、角度带禁用词、方向名不对、同车重复。`dropped` 不为 0 时，回头对照规则看是哪条被丢了。

## 好选题的标准（在规则之外，靠你判断）
- **角度是立场，不是话题。**"YU7 降价 2 万后，标准版反而最不值"是立场；"YU7 降价分析"只是话题。
- **钩子在 18 字以内**，并且后面写得出东西：事实收集阶段要能找到支撑它的数字。拿不准就先跑一次 `python -m app.cli search "<车型> <角度关键词>" --days 30`，看材料够不够。
- **为什么是现在**：`why_hot` 引用热点（榜单名次、新闻标题），建项目时传给 `--why`，写稿前的分析要回答它。`evidence` 的原文行逐条传给 `--evidence`，会当作一份事实材料进入抽取。
- 和账号最近做过的车型、角度错开：用 `python -m app.cli list` 看最近的项目。

## 交给用户
列出 3–5 个保留下来的选题，每个写清车型、角度、钩子、为什么是现在、建议时长和横竖屏，让用户挑。用户选定后：

```bash
python -m app.cli new "<subject>" --theme <theme> --angle "<angle>" --hook "<hook>" --why "<why_hot>" --search "<search_terms>" --evidence "<热点原文一行>" --duration 120
```

横评选题(theme 是横评方向):`subject` 写横评标题,车单每台一个 `--subjects "<车名>"`。
