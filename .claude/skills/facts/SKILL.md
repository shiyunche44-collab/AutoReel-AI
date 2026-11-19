---
name: facts
description: AutoReel 的事实层。回答事实抽取和口碑抽取请求，补材料、查缺口。适用于 step 停在 purpose 为 pipeline.intake.* 的请求，或者稿子缺数字、需要补事实的时候。
---

# facts：事实抽取与补料

**脚本里的每一个数字都必须能追到一条 Fact，而每条 Fact 都必须能追到原始材料里的原文。** 这是这个项目最硬的约束，后面的 grounding、lint 和标题校验都靠它。

## 回答抽取请求（`pipeline.intake.extract_facts`）
- 只从 USER 里的材料抽取，**不要用你自己知道的参数**，哪怕你确定它是对的。材料里没有，就不能有这条 fact。
- `source_tool`、`source_query`、`retrieved_at` 逐字照抄材料头。抄错了，这条事实就追不回原文。
- 数字逐字照抄材料，单位也照抄：不四舍五入，不换算（kWh 不改成度，万元不改成元）。
- `basis` 是口径：CLTC/WLTC、哪个版本、哪个年款。材料没写就留空，不要猜。
- 比较类事实要把两边的名字和两边的数都写全。只有对手一边数字的事实不要写。
- 技术机制（电机类型、电芯体系、悬架结构等），就算没有数字也单独写成一条，后面讲原理要用。
- 写完自查一遍：随便挑 3 条，回到材料里找原句，看能不能对上。

## 口碑抽取（`pipeline.intake.extract_reviews`）
只写转述，说清是谁说的："有车主说……""某媒体实测……"。不要把个人感受写成产品参数。

## 事实不够时
在抽取请求还没回答之前补，补进来的材料会进入抽取提示词：
```bash
python -m app.cli facts-raw <pid>                       # 现在有哪些材料、哪些来源失败了
python -m app.cli datapro specs "<车型>"                # 官方参数配置(方舟专业数据集)
python -m app.cli datapro sales "<车型>" --months 6
python -m app.cli search "<车型> 续航 实测" --days 180   # 豆包搜索
python -m app.cli facts-add-doc <pid> doc.txt --tool ark.search --query "<查询>"
python -m app.cli step <pid>                            # 出一个带新材料的抽取请求
```
- `doc.txt` 放**原文摘录**，每段开头标明来源和日期，例如"【懂车帝 2026-10-12】……"。不要放你自己的总结。
- `--tool` 写真实的来源名，例如 `ark.search`、`ark.datapro`、官网域名。它会进 `Fact.source_tool`。
- 已经过了 FACTS_RESOLVED 才发现缺数字：门禁 A 改稿时把这个点改成定性说法，不要硬补。

## 抽取之后
用 `python -m app.cli facts <pid>` 看事实和 `report`：
- `product_gap`：产品层还缺哪些维度。缺得多就回去补料。
- `scope_counts`：product 层要占多数。company 层（工商、财报）不能拿来当卖点。
