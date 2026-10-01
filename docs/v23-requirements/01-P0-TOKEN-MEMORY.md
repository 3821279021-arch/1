# P0：Token 与 Memory 优化

## 1. 修正 token 估算

### 现状
重点检查：`app/llm.py`

当前估算逻辑使用类似：

```python
128 + len(system.encode("utf-8")) + len(user.encode("utf-8"))
```

这不是 token 估算，而更接近 UTF-8 字节数。中文字符通常占多个字节，因此预算判断会偏保守。

### 建议

优先级从高到低：

1. 针对具体 provider 使用对应 tokenizer。
2. 如果不方便集成 tokenizer，则根据实测维护 provider/model 的经验系数。
3. 最后再增加 10%~20% safety margin，而不是直接把 byte 数当 token。

建议接口：

```python
def estimate_tokens(provider: str, model: str, text: str) -> int:
    ...
```

预算判断统一只调用这一个函数。

### 验收

- 对中文 prompt，估算值与真实 usage 的误差中位数 < 20%。
- 不再因为明显高估而提前触发 `game_token_budget` fallback。
- 记录 `estimated_input_tokens / actual_input_tokens` 指标。

---

## 2. 给长期记忆加上限

重点检查：`app/memory.py`

建议对以下集合设置长度上限或压缩策略：

- `claims`
- `check_claims`
- `stances`
- `contradictions`
- `vote_history`
- `received_votes`
- `self_history`
- `judgments`
- `conjectures`

不要只是简单 `[-N:]`；更好的方式是保留“摘要 + 最近记录”。

推荐结构：

```python
memory = {
    "summary": "长期摘要",
    "recent": [...],
    "confirmed_facts": [...],
    "current_suspicions": {...},
    "role_claims": {...},
    "vote_summary": {...},
}
```

### 压缩触发

满足任一条件即可压缩：

- 进入新的一天
- 某列表超过上限
- 估算 prompt token 超过阈值

### 建议默认值

```text
recent_events: 12
recent_claims: 12
recent_stances: 12
recent_votes: 18
recent_judgments: 10
```

历史部分折叠进 summary。

---

## 3. 增加“每日摘要”

一天结束后生成一次结构化摘要，而不是下一天继续携带全部原始记忆。

建议字段：

```json
{
  "day": 2,
  "confirmed_facts": [],
  "role_claims": {},
  "vote_patterns": {},
  "contradictions": [],
  "suspicions": {},
  "key_relationships": [],
  "self_plan": ""
}
```

如果不想再调用一次 LLM，可以先用服务器规则进行压缩。

---

## 4. Prompt 分层

建议把 prompt 分成：

1. 永久规则：角色、胜负条件、行为约束
2. 当前局状态：白天/夜晚、存活玩家
3. 长期摘要：固定长度
4. 最近事件：固定窗口
5. 当前任务：发言 / 投票 / 技能

尽量避免在不同层重复描述同一信息。

---

## 5. 成本观测

每次模型调用记录：

```text
room_id
agent_id
day
phase
provider
model
estimated_input
actual_input
output_tokens
latency_ms
fallback_reason
```

这样 V2.3 可以真正回答：

- 哪个阶段最贵？
- 哪类 Agent 最贵？
- 哪个 provider 最容易失败？
- Memory 压缩后节省多少？

---

## 目标

在同等游戏质量下：

- 长局 input token 至少下降 30%
- Mock fallback 明显下降
- 后期回合 prompt 不再线性增长
