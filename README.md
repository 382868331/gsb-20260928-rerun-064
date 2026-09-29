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

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖重复身份、相互矛盾日期、依赖成环、缺失依赖、缺失/脏值不按零值处理，
以及旧批次结论不被覆盖。
