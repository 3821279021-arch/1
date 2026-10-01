# AI 狼人杀 V2.3 改进建议包

本包基于 `ai-werewolf-v2.2` 的代码结构、测试与实测记录整理，目标不是重写项目，而是给出可以直接进入 V2.3 开发排期的修改方案。

## 推荐实施顺序

1. **P0：Token / Memory 成本控制**
   - 修正 token 估算方法
   - 给长期记忆设置上限
   - 引入阶段/每日摘要
   - 避免因高估 token 过早切到 Mock

2. **P1：模型层和网络层**
   - 复用 `httpx.AsyncClient`
   - 统一四家模型的结构化输出、超时、重试、usage 统计
   - 将“决策状态”和“LLM 表达”拆开

3. **P1：多人产品与安全**
   - 踢人、锁房、恢复码、token revoke
   - 房间级限流与身份恢复
   - 增加 CSP 等响应头

4. **P2：存储与前端可维护性**
   - 从整局 JSON 快照逐步拆到事件/记忆/会话
   - 加数据清理策略
   - 拆分 `app/static/app.js`

5. **P2：玩法扩展**
   - 9/12 人局、更多角色、房主规则配置
   - 观战、回放、成长统计

## 文件索引

- `01-P0-TOKEN-MEMORY.md`：最高优先级的 token 与记忆优化
- `02-P1-LLM-BACKEND.md`：模型路由、HTTP、结构化输出、后端扩展
- `03-P1-MULTIPLAYER-SECURITY.md`：多人房间、身份、安全与恢复
- `04-P2-FRONTEND-STORAGE.md`：前端拆分、SQLite 与数据生命周期
- `05-AI-STRATEGY.md`：让 AI 从“会说话”升级到“会推理和制定策略”
- `06-V23-ROADMAP.md`：建议的 V2.3 开发路线图
- `07-ACCEPTANCE-CHECKLIST.md`：修改完成后的验收清单

## 最值得先修的三项

### 1. Token 估算
`app/llm.py` 当前存在把 UTF-8 字节长度近似为 token 数的做法。中文场景下会明显高估，可能导致预算保护提前触发，使真实模型被过早降级为 Mock。

### 2. Memory 无限增长
虽然最近事件已有限制，但 `claims / stances / contradictions / vote_history / self_history / judgments` 等结构仍可能随局长持续增长，导致每轮 prompt 越来越大。

### 3. 单进程 + SQLite
当前架构适合 Demo / 小规模部署，但不适合多实例和更高并发。短期不必立刻换数据库，但应该先把状态、事件、记忆边界拆清楚，给未来 PostgreSQL / Redis 留迁移路径。

## 建议版本目标

V2.3 不建议以“新增更多角色”为主要目标。更适合作为一次稳定性与智能质量版本：

> **V2.3 = 更低成本 + 更稳定模型调用 + 更聪明 AI + 更可靠多人体验**
