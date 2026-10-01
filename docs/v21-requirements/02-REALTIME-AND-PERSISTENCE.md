# 02 — Realtime, Persistence and Reliability

## R-002 AI 流式发言必须轻量化

### 问题

AI speech streaming 期间，如果每个文本 chunk 都触发：

1. 整局状态写入 SQLite
2. 为每个客户端构造完整 snapshot
3. 广播完整 snapshot

则长发言会造成大量重复 IO、序列化与网络流量。

### 目标

流式文本属于“瞬时增量事件”，不应等价于完整游戏状态提交。

### 推荐协议

AI 流式期间仅发送：

```json
{
  "type": "speech_chunk",
  "game_id": "...",
  "turn_id": "day2-seat4",
  "player_id": 4,
  "delta": "我认为",
  "state_revision": 138
}
```

结束时再发送：

```json
{
  "type": "speech_completed",
  "player_id": 4,
  "content": "完整发言",
  "state_revision": 139
}
```

并在 completed 时正式持久化。

### 持久化要求

允许以下任一策略：

- 仅发言结束时写库；或
- streaming 时最多每 500~1000ms 做一次节流 checkpoint；发言结束强制写库。

禁止：

- 每个 token / 几个字符一次整局 SQLite save。
- 每个 speech chunk 都向所有客户端发送完整 state snapshot。

### 失败恢复

若 AI 流式中断：

- 已完成文本可选择保存为 partial speech；或
- 丢弃未完成 speech，但必须让状态机继续前进。
- 不得永久卡死 turn scheduler。

---

## R-008 增加 state_revision 与 action_id

### state_revision

每次权威 GameState 发生 mutation 时：

```text
state_revision += 1
```

完整 snapshot 必须包含：

```json
{
  "game_id": "...",
  "state_revision": 139,
  "turn_id": "day2-seat4",
  "phase": "DAY_SPEECH",
  "server_time": "..."
}
```

客户端：

- 只应用 revision >= 当前 revision 的新状态。
- 旧 snapshot / 旧 event 不得覆盖新状态。

### action_id 幂等

所有会改变状态的用户操作应携带：

```json
{
  "action_id": "uuid",
  "expected_state_revision": 139,
  "turn_id": "day2-vote",
  "action": "vote",
  "target_player_id": 3
}
```

服务端应：

- 对已处理 `action_id` 返回原结果，不重复执行。
- 若 `expected_state_revision` 明显过期，拒绝或要求客户端刷新。
- 若 `turn_id` 已结束，必须拒绝。

### AI 结果过期保护

AI 请求发出时记录：

- game_id
- turn_id
- state_revision

模型响应回来后，若当前 turn / revision 已不再适用：

- 丢弃结果
- 记录 `stale_ai_response`
- 不得修改 GameState

### WebSocket 事件要求

事件应记录“生成时”的 revision，而不是发送时动态读取当前 revision。

---

## 连接恢复

手机断线重连流程：

1. 使用 session / reconnect token 重新认证。
2. 服务端发送最新完整 snapshot。
3. 客户端以 snapshot 的 state_revision 作为新基线。
4. 之前排队的旧增量 event 不得再次覆盖当前状态。

## 性能验收

以一段 120 字 AI 流式发言为例：

- SQLite 整局保存次数应从“按 chunk 多次”降为 1~少量节流次数。
- 完整 snapshot 广播次数不得与 speech chunk 数量线性增长。
- 客户端仍然可以逐字 / 分块看到流式效果。
