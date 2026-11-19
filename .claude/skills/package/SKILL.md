---
name: package
description: AutoReel 发布包装，包括 3 条标题、置顶评论和备用回复。适用于合成阶段 step 停在 purpose 为 pipeline.packaging.generate 的请求，或者出片后要改标题、评论的时候。
---

# package：标题、置顶评论、备用回复

合成（COMPOSING）时会自动出一个包装请求，用 `ask show` 看、`ask answer` 答。出片以后想重写：
```bash
python -m app.cli package <pid>              # 现在的包装
python -m app.cli package-prompt <pid>       # 提示词原文(事实、稿子、规则)
python -m app.cli package-put <pid> p.json   # 重写;不合格的条目由模板顶上
```
`package-put` 返回的 `replaced_by_template` 不为空，说明你写的那几条没通过校验，被模板替换了。对照下面几条找原因，改完再交一次。

## 校验最常拦的几条
- **单车片的标题必须以完整车名开头**，否则直接丢掉。账号九成播放来自搜索。
- **标题里的数字要逐字出自事实或稿子**，不能四舍五入，也不能张冠李戴：A 车的数字不能用在 B 车的标题里。
- 标题字数有上限，不能有禁用词，不能是关键词堆砌。
- 置顶评论的两个选项要原样出现在 `text` 里，并且和片尾提的问题一致。
- 备用回复只准用事实里的数字，语气要平和。

## 写得好的标准
- 三条标题（stake / number / person）是三种不同的写法，不是同义改写。
- 标题是主论点换一种说法，不引入片里没有的判断。
- 原理拆解片（title_style=question）写成问题句，不要写成导购标题。
- 交给用户时把 3 条标题都列出来，并标出你推荐哪条、为什么。最后用哪条由用户在 Web 上或对话里决定。
