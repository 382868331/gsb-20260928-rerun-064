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

## 依赖下游与影响预览（第四轮）

新规则类型 `group_sum`：先由上游身份规则确认记录身份，再按确认后的
分组对数值字段求和并约束总额（`params`: `key_fields` / `sum_field` /
`max_total` / `min_total`）。身份未确认的记录不参与分组求和；缺失或
非数值的金额按 `missing_value` / `malformed_value` 报告，绝不按 0 计入。

上游结论未更新或执行失败时，下游按身份组区分范围：

- 与上游未确认范围不重叠的分组仍可复用旧结论并照常重判；
- 重叠的分组本轮**不能重新确认**：保留旧结论作为历史，规则列入
  `incomplete_rules`，错误信息列出未确认的记录，运行状态为 FAILED——
  不会借旧分组把本轮标为通过；
- 结果 JSON 的 `plan` 中每条规则带 `blocked`（本轮未确认的记录）与
  `upstream`（每个上游结论实际采用的运行：`carried` 指沿用上轮运行，
  `current` 指本轮重判），可逐条追到下游结论使用的上游版本。

影响预览（不执行检查，只报告影响面）：

    python -m dq_platform.cli --sample samples/payments_v2.json --prev-sample samples/payments_v1.json --rules samples/rules_deps.json --preview-impact --changed-field txn_ref

沿依赖图向下游传播，同一受影响范围只计一次；依赖成环、依赖缺失或
指定的规则不存在都会给出明确错误并以退出码 1 结束。恢复运行会对
未确认范围全量重判，与完整重算结果一致，已解决的问题在生命周期
账本中只记一次 `resolved`。

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

第四轮新增：`group_sum` 按确认后的身份分组计算总额约束、上游中断时
下游按组区分可复用与不可重新确认的范围、影响预览与上游版本追溯、
多上游同时变化时范围去重、身份拆分后重新合并与恢复运行的结果均与
完整重算一致，均有测试覆盖。
