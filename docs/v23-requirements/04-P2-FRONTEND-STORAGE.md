# P2：前端、存储与生命周期

## 1. 拆分 `app/static/app.js`

建议最低限度拆成 ES Modules：

```text
static/js/api.js
static/js/socket.js
static/js/store.js
static/js/lobby.js
static/js/game.js
static/js/pet.js
static/js/audio.js
static/js/pwa.js
static/js/main.js
```

不用为了拆分而立即引入 React/Vue。

重点目标：

- 网络层不直接操作 DOM
- UI 不直接拼请求
- WebSocket 只负责事件输入
- store 作为单一客户端状态源

---

## 2. 从“整局 JSON”向事件化演进

当前可以继续保留 snapshot，但建议增加事件日志边界：

```text
room_snapshot
room_events
player_memory
sessions
```

写入策略：

```text
事件发生
→ append event
→ 更新内存状态
→ 周期性 snapshot
```

好处：

- 回放更容易
- 崩溃恢复更清晰
- 分析游戏行为更容易
- 将来迁移 PostgreSQL 更容易

---

## 3. 数据真正清理

不能只把状态改成 `ARCHIVED`。

建议配置：

```text
ACTIVE / FINISHED：完整保留
ARCHIVED 7~30 天：保留完整快照
超过期限：删除聊天、私有记忆等详细数据
只保留匿名统计
```

实现清理任务：

```python
cleanup_expired_rooms()
cleanup_expired_sessions()
```

---

## 4. 可观测性

至少增加结构化日志：

```text
room_created
player_joined
phase_changed
llm_called
llm_failed
fallback_triggered
room_finished
room_archived
```

将 `room_id` / `agent_id` / `request_id` 放入每条日志上下文。

---

## 5. 依赖与部署

建议增加锁定机制：

- `requirements.txt` 固定关键生产依赖版本，或使用 `uv.lock` / `poetry.lock`
- CI 自动运行测试
- CI 执行 Python compile / JS syntax check
- Docker build smoke test

避免未来一次依赖自动升级导致线上行为变化。
