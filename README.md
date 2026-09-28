# 数据质量规则平台（第一轮）

本轮只读**本地人工样本**（JSON），先建立三件基础数据：

- **数据批次（Batch）**：一次检查对应一个不可变批次，带数据指纹。
- **规则版本（Rule Version）**：规则带 `version` 标识；修订规则后重新检查必须生成新批次。
- **问题记录（Issue）**：每条问题指回具体 `record_id`、规则/版本与违反条件；跨记录问题通过 `related_record_ids` 关联冲突记录。

## 校验能力

- 跨字段：`CrossFieldRule`（任意谓词）、`DateContradictionRule`（日期矛盾）。
- 跨记录：`DuplicateIdentityRule`（身份键查重）。
- 规则依赖：执行前做拓扑排序；**缺失依赖**或**依赖成环** → 批次状态为 `error`。
- 三态单元格：`ok / missing / malformed`。缺失值与格式错误**不会**被当成 0、空串或 False，而是产生“字段不可用”问题。
- 执行失败（规则抛异常、依赖问题）时批次状态为 `error`，**绝不输出“全部合格”**。

## 使用

```bash
PYTHONPATH=. .venv/bin/python -m dq_platform.cli samples/employees.json B-2026-001 --rule-version rules-v1
```

退出码：`0` 全部合格；`2` 发现数据问题；`3` 执行失败。

批次结论归档到 `batch_reports/<batch_id>.json`；同一批次重复归档会被拒绝，
修订规则后请使用新批次号（旧批次结论保持只读）。

## 测试

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

覆盖：重复身份、日期矛盾、依赖缺失/成环、缺失与格式错误三态、执行失败不得判合格、旧批次不可覆盖。

## 后续扩展

新数据来源在 `dq_platform/loader.py` 增加读取器，输出统一的 `(record_id, {field: Cell})` 结构即可复用引擎。
