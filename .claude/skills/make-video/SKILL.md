---
name: make-video
description: 用 AutoReel 做一部产品解说视频，Claude Code 负责推进和写作。适用于用户说"做一期 / 做个视频 / 出片 / 继续那个项目"或给出车型与角度的情况。在对话里完成事实抽取、写稿、分镜、门禁 A/B 和审片，工具层是 `python -m app.cli`。
---

# make-video：Claude 驱动出片

整条流水线的状态机、校验、渲染和合成都不变。区别是**所有文字模型调用都改成由你来回答**，后台调度器不碰这个项目（`projects.driver = 'claude'`）。工具层输出一律是 JSON，退出码含义如下：

| 码 | 含义 | 你该做什么 |
|---|---|---|
| 0 | 成功 | 看 `state` 决定下一步 |
| 2 | 被拒（lint、门禁、状态不对、项目被占） | 读 `error` 修好再来，**不要**加 `--force` 硬闯 |
| 3 | 有请求等你回答 | `ask show` 看完再 `ask answer` |
| 1 | 其它错误 | 读报错；网络类可以重试一次 |

命令统一写成 `python -m app.cli <cmd> ...`。在 Bash 里中文参数没问题，长 JSON 先写进 scratchpad 文件再传路径（`-` 表示从 stdin 读）。

## 开工前
1. 确认 8848 跑的是新代码：调度器认 `driver` 列要靠新代码，旧进程会抢推进。拿不准就让用户用 `scripts/restart_server.ps1 -Reason "..."` 重启，**自己别杀进程**。
2. 选题和角度跟用户对齐：车型、时长、横竖屏（建好后不能改）、主题（buyguide 等）、角度一句话。还没定车型就先走 **topic** skill。

## 请求归哪个 skill（看 `needs_claude.purpose`）
| purpose 前缀 | 阶段 | skill |
|---|---|---|
| `pipeline.intake.` | 事实抽取、口碑转述 | **facts** |
| `pipeline.script_gen.` | 大纲、钩子、全文、压字数、口语化、自修 | **script** |
| `pipeline.explainer.` | 分镜版式、关键词拍 | **storyboard** |
| `pipeline.edit.match.` | 渲染期剪辑取材：给每一刀挑素材 | 按提示词挑出最能让观众"看到这半句在讲什么"的那条 |
| `pipeline.packaging.` | 标题、置顶评论、备用回复 | **package** |
| 其它 | — | 照 SYSTEM 回答，说不准就先 `ask show` 读完全文 |

每一类都先读对应的 skill 再回答。提示词本身就是最终规范，skill 只补充"怎么做得好"和"最常在哪儿被拦"。

## 主循环
```bash
python -m app.cli new "<车型>" --duration 120 --theme buyguide --angle "<角度>" [--aspect 9:16]
python -m app.cli step <pid>             # 一直推进，直到要你回答、到门禁或出片
python -m app.cli ask show <pid>         # 退出码 3 时：读 SYSTEM/USER 原文与必需字段
python -m app.cli ask answer <pid> <id> answer.json
python -m app.cli step <pid>             # 重跑这一步；答过的请求按提示词指纹直接复用
```
- **回答就是那个提示词要求的 JSON**：必需字段齐全，结构照 SYSTEM 里的说明写。字段缺了 `ask answer` 会拒收（退出码 2）。
- 提示词里的写作规范（口语化、数字要引用事实 id、去 AI 味、品类 vocab）**照做**，回答完的内容还会再过 grounding 和 lint 校验，过不了照样被打回。
- **数字只能来自事实**：抽取事实时 `basis` 和 `source_kind` 照材料原文填，材料里没有的数字一律不写。写稿时每个数字都要挂上 `fact_refs`。
- 同一步可能连着出好几个请求（事实抽取 → 写稿 → 自审 → 分镜……），一个一个答就行。`step` 输出里的 `pending_asks` 列着还没答的。
- 原始材料只采集一次，缓存在 `<项目>/_raw_cache/`，所以重跑同一步时提示词保持不变。

## 事实不够时（FACTS_RESOLVED 之前，抽取请求还没答的时候）
```bash
python -m app.cli facts-raw <pid> [--full]        # 已经采到哪些原始材料
python -m app.cli search "<查询>" --days 90       # 豆包搜索(方舟)
python -m app.cli datapro specs|sales "<车型>"    # 方舟专业数据集
python -m app.cli facts-add-doc <pid> doc.txt --tool claude.research --query "<查询>"
python -m app.cli step <pid>                      # 材料变了 → 会出一个新的抽取请求
```
补进来的材料必须是查到的原文摘录，并注明出处，不要自己写总结冒充材料。抽取完成后用 `facts <pid>` 看事实和采集报告（product_gap 表示缺口）。

## 门禁 A：稿子和分镜
```bash
python -m app.cli script <pid>           # 稿子 + lint
python -m app.cli shots <pid>            # 分镜
python -m app.cli lint <pid>             # 放行前的拦截项
python -m app.cli script-put <pid> s.json   # {"segments":[...],"title":"","thesis":""}
python -m app.cli shots-put <pid> shots.json
```
先自己审一遍：钩子在前 3 秒、论点清楚、有没有无来源的数字、节奏、版式重复度。改完让 `lint` 通过，**再把标题、论点、分段摘要和你改过的地方交给用户看**。用户在对话里明确同意之后才执行：
```bash
python -m app.cli step <pid> --confirm
```
`--force`（带着 lint error 放行）只有用户明确说"就这样放行"时才用。

## 渲染期间
`step` 每轮渲染几拍，会自己循环。整片比较长时可以 `--max 200` 让它跑在后台（Bash 的 run_in_background），完成通知到了再继续。用 `status <pid>` 看进度和最近事件。

## 门禁 B：审片
```bash
python -m app.cli review <pid>           # 预览、拍进度、问题清单
python -m app.cli cuts <pid>             # 每一刀用的素材
python -m app.cli swap <pid> <key>       # 换一刀
python -m app.cli rerender <pid> 3 5     # 重渲第 3、5 拍
```
先自审：截帧拼图（用 Read 看图）、音频问题、和对标基线比。再把预览路径和你改过的地方交给用户。用户同意后执行 `step <pid> --confirm`，进入合成并出片。

## 规矩
- 门禁只在用户明确同意后才 `--confirm`。每次 `step` 最多放行一道门禁。
- 别用 Web 的门禁按钮推进 Claude 项目，也别对它调 `/advance`。
- 想交还给后台调度器：`python -m app.cli adopt <pid> --release`。旧项目要接手：`adopt <pid>`。
- 你自己的 token 不进 `llm_calls` 账本；方舟的 TTS、搜索、数据集、生图照常记账（`/api/llm/usage`）。
- 降级（概念动画退回数据卡等）记在 `/api/degradations`，审片时去看一眼。
