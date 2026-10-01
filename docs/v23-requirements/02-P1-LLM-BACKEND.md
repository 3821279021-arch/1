# P1：LLM 与后端层改进

## 1. 复用 HTTP Client

重点：`app/llm.py`

不要每次调用都：

```python
async with httpx.AsyncClient(...) as client:
    ...
```

建议让 `LLMRouter` 或应用生命周期持有一个长期 `AsyncClient`：

```python
class LLMRouter:
    def __init__(self):
        self.client = httpx.AsyncClient(...)

    async def close(self):
        await self.client.aclose()
```

收益：

- keep-alive
- TLS 连接复用
- 更低延迟
- 降低高频调用开销

---

## 2. 统一 Provider 接口

建议抽象：

```python
class ProviderAdapter(Protocol):
    async def generate(...): ...
    async def stream(...): ...
    def estimate_tokens(...): ...
    def normalize_usage(...): ...
```

OpenAI / Claude / Gemini / DashScope 都输出统一结果：

```python
LLMResult(
    text=...,
    parsed=...,
    input_tokens=...,
    output_tokens=...,
    latency_ms=...,
    provider=...,
    model=...,
)
```

---

## 3. 统一结构化输出

当前不同 provider 对 JSON 的约束程度不同。

建议所有“动作决策”尽可能使用 schema，而不是只依赖 prompt：

```json
{
  "action": "vote",
  "target": 3,
  "reason": "..."
}
```

并统一验证：

1. JSON parse
2. schema validate
3. 规则层 validate
4. 无效时进行一次 repair / retry
5. 仍失败才 fallback

不要直接把 provider 返回的结构当合法游戏动作。

---

## 4. 决策与表达分离

目前 LLM 同时承担：

- 判断局势
- 制定策略
- 选择动作
- 生成自然语言

建议拆为：

```text
GameBeliefState
    ↓
StrategyPolicy
    ↓
ActionDecision
    ↓
LLM Expression
```

即：服务器先形成结构化决策，再让模型用角色口吻说出来。

这样 provider fallback 时，不会整个 Agent 性格和策略突然变化。

---

## 5. 后端可扩展性

短期仍可保留 SQLite，但需要先做到：

- 数据访问层独立
- scheduler 不依赖进程内唯一对象
- 房间状态可以从持久化恢复
- 避免 async 路径里大量同步磁盘 IO

后续演进：

```text
SQLite -> PostgreSQL
进程内状态 -> Redis / distributed coordination
单 worker -> 多 worker
```

不建议 V2.3 一次性全部迁移；先把接口边界做对。
