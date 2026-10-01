# AI Strategy：从“会说话”到“会玩”

## 目标

LLM 不再每轮只从自然语言历史重新理解局势，而是使用服务器维护的结构化世界模型。

## 建议新增 `GameBeliefState`

每个 Agent 维护：

```python
class GameBeliefState:
    alignment_probabilities: dict[int, float]
    role_probabilities: dict[int, dict[str, float]]
    credibility: dict[int, float]
    claim_consistency: dict[int, float]
    vote_pressure: dict[int, float]
    relationships: dict[int, str]
    strategy_plan: str
```

注意：这里的概率不必一开始就做复杂 Bayesian 模型，可以先是可解释的启发式评分。

---

## 信息来源权重

例如：

```text
明确查验结果          高权重
公开身份冲突          高权重
投票行为              中高权重
自相矛盾              中权重
普通发言怀疑          低到中权重
单纯跟风              低权重
```

并加时间衰减：旧怀疑不应该永久累加。

---

## 角色约束

服务器可以确定性维护：

- 谁 claim 了预言家
- claim 数量是否冲突
- 谁给过什么查验
- 谁在什么时间投过谁
- 哪个信息是在死亡前/后说的

不要让 LLM 自己从完整聊天中反复解析。

---

## 狼人策略

狼人应该额外有隐藏计划：

```text
kill_target
fake_claim_plan
teammate_distance_strategy
push_target
risk_level
```

但仍必须通过 `InformationScope` 保证不会泄漏给好人阵营。

---

## 发言生成

先有决策：

```json
{
  "stance": "suspect",
  "targets": [4],
  "confidence": 0.72,
  "intent": "push_vote",
  "points": [
    "D1 与 D2 站边变化",
    "投票与发言不一致"
  ]
}
```

再交给 LLM：

> 用 3 号玩家的性格，把这些观点表达成 2~4 句话，不要添加新事实。

这样语言可以换模型，但核心逻辑保持稳定。

---

## 语义抽取

当前正则可继续作为第一层：

```text
明确格式 -> regex 快速抽取
自然表达 -> 小模型 / 主模型结构化抽取
```

例如识别：

- “2号怎么看都不太对”
- “4大概率进狼坑”
- “2、5至少出一狼”

抽取结果必须标注来源和置信度，不能把模型推测直接升级为事实。
