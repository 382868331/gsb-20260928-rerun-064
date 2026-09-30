# 数据质量规则平台（第二轮：增量检查与问题生命周期）

只读本地人工样本的 Python 数据质量平台。数据批次（batch）、规则版本集
（rule version set）与问题记录（issue）三层分开；规则可声明依赖，
引擎先做依赖检查再按拓扑序执行。

## 运行

```bash
python -m dq_platform.cli --sample samples/employees_v1.json --rules samples/rules_v1.json --out results
python -m dq_platform.cli --sample samples/clean.json --rules samples/rules_v1.json --out results
```

增量检查：提供上一份样本快照，引擎只重判实际受影响的记录，跨行约束会自动
带上与其同组的伙伴，并以上一次完整成功的结论为基线：

```bash
# 1) 首次完整检查
python -m dq_platform.cli --sample samples/employees_v1.json \
  --rules samples/rules_v1.json --batch-id emp --out results
# 2) 修改少量记录后增量检查
python -m dq_platform.cli --sample samples/employees_v2_fixed.json \
  --baseline-sample samples/employees_v1.json \
  --rules samples/rules_v1.json --batch-id emp --out results
# 3) 规则条件改变（leave_date 不再可选）：触发该规则全量重判
python -m dq_platform.cli --sample samples/employees_v1.json \
  --rules samples/rules_v2_leave_required.json --batch-id emp --out results
```

失败的增量运行不会被选为新基线（基线必须没有未完成范围）。

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

## 增量检查与问题生命周期

- **受影响范围**：记录按 `record_id` 与数据内容做 diff；`required_fields`
  / `date_order` 只重判改动记录；`unique_fields` 额外重判与改动记录同键的
  现伙伴，以及基线旧分组中的前伙伴（含已删除记录）。依赖规则继承前置规则
  的重判范围。增量结论在样本与规则上与完整重算逐问题一致。
- **问题台账**（`results/issues_snapshot.json` 与 `issue_events.jsonl`）：
  问题以 `rule|record|field|kind` 为稳定键跟踪 `open/resolved`。已解决问题
  不会从历史消失，保留首次发现以及解决时的运行与规则版本；问题再次出现记
  为 `reopened`（沿用同一条历史记录），规则判断条件改变记为
  `condition_changed`，与“重新出现”明确区分。
- **执行失败**：规则执行异常时保留该规则上一份有效结论，结果的
  `incomplete_scope` 明确列出本次未完成的规则、原因与记录范围；未完成规则
  不产生任何生命周期事件，台账状态保持为上一份有效结论。
- **结果字段**：运行结果新增 `mode`、`affected_records`、
  `deleted_record_ids`、`incomplete_scope`、`rule_signatures`、
  `reused_baseline_run_id`。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖重复身份、相互矛盾日期、依赖成环、缺失依赖、缺失/脏值不按零值处理、
旧批次结论不被覆盖；增量与完整重算一致、跨行唯一性伙伴连带重判、删除释放
伙伴、问题解决/重现/条件变更的生命周期，以及规则失败时保留旧结论并标注未
完成范围。
