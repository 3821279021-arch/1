# 04 — Security, Cost and Room Lifecycle

## R-004 公网部署必须增加限流和费用保护

### 风险

公开部署后，匿名用户可重复：

- 创建 session
- 创建房间
- 使用 AI 宠物聊天
- 触发 AI 玩家发言 / 决策
- 消耗服务端配置的大模型额度

仅限制并发不能限制总成本。

### 最低要求

至少增加以下限制：

- 单 IP / session 创建速率限制
- 单用户 / session 建房速率限制
- 单房间 AI 调用频率限制
- Pet Chat 调用频率限制
- 单局最大 AI 请求数量
- 单局最大 token / 成本预算
- 服务端全局每日预算软上限
- 最大同时活跃房间数

### 超限行为

达到限制时：

- 返回明确的限流错误
- 不得继续调用第三方模型
- 游戏核心状态不得因此损坏
- 可选自动 fallback 到 Mock，但必须对房主透明

### 日志

至少记录：

- provider
- model
- request category
- latency
- input/output token（可获得时）
- estimated cost（可获得时）
- fallback reason
- room_id / game_id（不要记录敏感 token）

### Secret 安全

- API Key 只能存在服务器环境变量。
- 不得发送到浏览器。
- 不得写入 snapshot、WebSocket event 或公开日志。
- 错误返回不得包含完整 Key 或第三方授权 header。

---

## Session / 座位所有权

所有修改状态的请求必须验证：

- 当前 session 是否属于该玩家
- 该玩家是否拥有该 seat
- 当前 phase 是否允许此动作

客户端传来的 `player_id` / `seat` 不能作为唯一信任依据。

推荐：

```text
session_token -> room membership -> player_id -> seat ownership
```

刷新页面和断线后，用户应可通过合法 session / reconnect token 恢复原座位。

---

## R-009 房间生命周期管理

### 问题

如果所有历史房间一直加载到内存，后台 scheduler 持续扫描，长期公开运行会积累无效房间。

### 状态

推荐房间生命周期：

```text
LOBBY
ACTIVE
FINISHED
ARCHIVED
DELETED
```

### 内存管理

- 长时间无连接且非 ACTIVE 的房间可以从内存卸载。
- 新请求访问时可按需从 SQLite 恢复。
- scheduler 不应高频遍历长期 finished / archived 房间。

### 清理建议

可配置：

- 空 LOBBY 房间数小时后归档 / 删除
- FINISHED 房间 7~30 天后归档或删除
- session / identity 设置过期时间

具体默认值可由部署环境配置。

### 用户功能

建议支持：

- 房主关闭房间
- 游戏结束后“再来一局”
- 复用同一批玩家创建新 game_id
- 不复用上一局私有角色状态 / AI memory，除非产品明确要求长期人格记忆

---

## Debug / Replay 建议

服务端建议记录权威 event log：

```text
#001 room_created
#002 player_joined
#003 game_started
#004 role_assigned
#005 wolf_message
#006 wolf_action
#007 seer_action
#008 speech
#009 vote
...
```

开发模式应可追踪：

- 当前 GameState
- 某玩家 PlayerView
- AI 输入上下文摘要
- AI 原始输出
- RuleEngine 接受 / 拒绝原因
- state_revision 变化
- provider / latency / tokens

这部分主要用于快速定位 InformationScope 泄漏、状态机竞态和 AI 非法输出。
