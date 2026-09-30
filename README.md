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

- 行级规则（`required_fields`、`date_order`）只重判改动/新增的记录；
  跨记录规则（`unique_fields`）因分组可能整体变化而全量重判该规则；
  依赖被全量重判的规则会级联全量。未触及的规则直接沿用上次结论。
- 规则版本集变化（判断条件改变）时自动回退为完整重算。
- 规则执行中途失败：该规则保留上一份有效结论，运行结果的
  `incomplete_rules` 与错误信息明确列出本次没有完成的范围，状态仍为
  FAILED，绝不会报“全部合格”。

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
