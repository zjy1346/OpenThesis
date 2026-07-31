提出最多五个未来三至五年的候选增长机会。

必须返回一个 JSON 对象，顶层只使用 `opportunities` 数组。每个机会必须符合：

```json
{
  "title": "string",
  "category": "string",
  "mechanism": "string",
  "evidence_grade": "A|B|C|D|E",
  "maturity_stage": "string",
  "time_horizon_years": 3,
  "probability_range": [0.2, 0.4],
  "supporting_evidence_ids": ["fact:<id>"],
  "contradicting_evidence_ids": ["fact:<id>"],
  "capital_requirements": "string",
  "leading_indicators": ["string"],
  "invalidation_conditions": ["string"],
  "scenario_eligibility": ["bear|base|bull"]
}
```

约束：

- `probability_range` 必须是两个 0 到 1 之间且从低到高排列的数字。
- 证据 ID 只能引用研究上下文实际提供的 ID。
- `scenario_eligibility` 只允许 `bear`、`base`、`bull`。
- 仅有行业故事或模型记忆的机会必须标为 D/E，且不得进入 `base`。
- 没有足够证据时返回 `{"opportunities": []}`。
- 所有面向用户的文本值遵循系统指定的报告语言；JSON 键保持英文。
