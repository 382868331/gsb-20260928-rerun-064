# 数据质量规则平台（第一轮）

只读本地人工样本的 Python 数据质量平台。数据批次（batch）、规则版本集
（rule version set）与问题记录（issue）三层分开；规则可声明依赖，
引擎先做依赖检查再按拓扑序执行。

## 运行

```bash
python -m dq_platform.cli --sample samples/employees_v1.json --rules samples/rules_v1.json --out results
python -m dq_platform.cli --sample samples/clean.json --rules samples/rules_v1.json --out results
```

校验失败时进程退出码为 1，并输出每条问题指向的记录、字段与违反条件；
依赖缺失、依赖成环或规则执行异常同样判为 FAILED，不会输出“全部合格”。

## 规则类型

- `required_fields`：跨字段必填，缺失记为 `missing_value`。
- `unique_fields`：跨记录身份唯一（默认按 `fields`，可用 `params.key_fields`）。
- `date_order`：跨字段日期先后，如 `hire_date <= leave_date`；无法解析记为
  `malformed_value`。
- `grouped_sum`：按字段分组后校验金额合计，可配置 `group_fields`、
  `amount_field`、`min_total` 与 `max_total`。该规则依赖身份类上游，
  只有依赖范围内的分组结论已经在本轮确认后才会计算，绝不拿旧分组冒充通过。

缺失值（`MISSING`）与格式错误（`MALFORMED`）是独立哨兵，参与比较会抛
`TypeError`，绝不被当作 0/空串参与判断。

## 不可变结论

结果以 JSONL 追加写入 `results/runs.jsonl`，键为 `batch_id + 规则版本集 +
时间戳`。修订规则后用新的 `version_set` 重新检查，旧批次结论保留不变。

## 增量检查（第二轮）

同一批次只改了少量记录时，用 `--prev-sample` 指向上一次样本，引擎按差异
决定每条规则的重判范围：

```bash
python -m dq_platform.cli --sample samples/employees_v2.json --batch-id employees_v1 --prev-sample samples/employees_v1.json --rules samples/rules_v1.json --out results --lifecycle
```

- 行级规则（`required_fields`、`date_order`）只重判“相关字段确实变了”
  的记录与新增记录；只改日期不会触碰身份规则，反之亦然。
- 跨记录规则（`unique_fields`）按身份组限定范围：只有键被改动/新增/
  删除记录碰到的组才重判，未受影响的分组直接沿用旧结论；受影响组会
  整组重建，因此结果与完整重算完全一致，不会漏掉跨行冲突。
- 规则定义变化、依赖被全量重判、或上一轮该规则未完成的，都回退为该
  规则全量重判——陈旧的沿用结论绝不被当作本轮结论。
- 规则执行中途失败：该规则保留上一份有效结论，运行结果的
  `incomplete_rules` 与错误信息明确列出本次没有完成的范围，状态仍为
  FAILED，绝不会报“全部合格”；无关规则照常完成。

## 检查计划前后对照（第三轮）

增量运行会输出（并写入结果 JSON 的 `plan` 字段）每条规则的范围与理由，
与上一轮结论（completed / INCOMPLETE）逐条对照：

- `skip`：变化内容与该规则无关，直接沿用；
- `records`：列出重判的记录及各自被修改的相关字段；
- `groups`：列出受影响身份组覆盖的记录，并说明是新增、删除还是
  字段修改（含修改了哪些字段）触发；
- `full`：说明触发原因（规则定义变化 / 依赖级联 / 上轮未完成恢复）。

恢复语义：某规则上轮失败后，本轮即使数据零变化也会对该规则全量重判；
恢复运行与完整重算结果一致，生命周期账本中已解决的问题只记一次
`resolved`，不会重复计入，旧失败也不会被当作本轮通过。

## 问题生命周期

`--lifecycle` 把每次运行折叠进 `results/issues.jsonl` 追加账本：

- `opened` / `resolved`：解决的问题保留原始记录，并记录解决它的
  运行与规则版本，不会从历史中消失。
- `reopened`：同一问题（规则+记录+字段+类型）再次出现记为重新打开，
  而不是新问题。
- `condition_changed`：问题仍在但规则版本集已变，区分“判断条件改变”
  与“问题重新出现”。
- 执行失败的规则：其未结问题标记为 `unknown`，既不被误记为已解决，
  也不会被重复打开。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖重复身份、相互矛盾日期、依赖成环、缺失依赖、缺失/脏值不按零值处理，
以及旧批次结论不被覆盖。

第二轮新增：增量结果与完整重算一致、仅重判受影响范围、问题解决/重开/条件
变化的生命周期，以及中途失败保留旧结论并标明未完成范围，均有测试覆盖。

第三轮新增：按变化内容限定检查范围（身份唯一性按受影响身份组重判而非
全量）、检查计划前后对照与逐条理由、失败恢复后全量重判且已解决问题不
重复计入，均有测试覆盖。

## 依赖版本与影响预览（第四轮）

每个规则结论按“记录单元”或“分组单元”保存版本：

- 行级规则的单元是单条记录；跨记录规则的单元是一个身份/业务分组。
- 每个下游单元在 `conclusions[].basis_versions` 中记录实际采用的上游
  规则、单元版本和覆盖记录；`status=blocked` 表示该单元本轮不能重新确认。
- 上游执行失败或上游依据缺失时，依赖链上的受影响记录/分组会标记为
  `blocked`，错误信息列出不能确认的范围；无关规则和无关分组照常完成。
- 下游分组规则会使用已确认上游单元计算；blocked 单元中的旧问题只作为历史
  证据保留，不会让本轮结果标为通过，也不会被误记为解决。

修改规则或字段前可以预览影响：

```bash
python -m dq_platform.cli \
  --sample samples/department_amounts.json \
  --rules samples/rules_dependent_sum.json \
  --out results \
  --preview-rule department_identity_present
```

预览沿 `depends_on` 反向传播，输出每个受影响规则、范围、去重后的记录集合
和依赖路径；多个上游或多条依赖路径指向同一规则/范围时只计一次。配合
`--prev-sample` 会读取上一轮结果，并显示该范围内下游当前实际引用的上游
结论版本。依赖成环由 `topo_order` 抛错并使运行 FAILED；缺少依赖或缺少可
追溯依据会分别在错误或 `warnings` / `missing_basis` 中明确反馈。

身份拆分后重新合并、上游中断、恢复后继续核对以及无关规则正常完成的完整
过程见 `tests/test_dependency_versions.py`。

## 撤回与替换材料（第五轮）

来源方的撤回、替换、拆分与合并不是普通字段改值。平台用
`dq_platform.corrections` 单独描述材料关系：

- `Correction` 明确列出撤回的旧记录、替换后的新记录、新记录继承的来源、
  更正依赖以及后续撤回的更正；`Acceptance` 表示使用者明确接受哪些材料。
- 一条旧记录可拆成多条，多条旧记录也可合并为一条；未明确接受的替换
  记录不会进入有效批次，也不能与当前有效资料混在同一次通过结论中。
- `IssueMigration` 区分 `same_fact`（同一事实，需要人工理由）与
  `review_new_fact`（需要重新审查的新事实）；拆分时一个旧问题不会因为
  多个目标而重复解决，合并时各旧问题的原理由在 `source_reasons` 中保留。
- 撤回关系形成循环、同一旧记录被互斥替换同时引用、更正依赖尚未接受，
  或部分替换未接受时，仅受影响身份/业务分组和下游结论标记为 blocked；
  无关检查仍可独立完成。
- 即使业务值完全相同，来源关系变化也会强制重新确认身份检查、分组总额
  和依赖依据；`accepted_material.lineage` 保存来源、关系和材料指纹。
- `compare_effective_results` 分开报告业务值变化、来源关系变化和人工
  决定迁移；后来撤回更正时，`decisions_without_basis` 可列出重新失去
  依据的人工决定，问题账本写入 `basis_lost`。

推荐入口是 `evaluate_corrections(...)`。先得到只包含已接受材料的有效批次，
再做增量检查；结果中的 `accepted_material`、结论版本和问题生命周期均对应
同一组已接受材料。完整端到端场景见 `tests/test_corrections.py`。
