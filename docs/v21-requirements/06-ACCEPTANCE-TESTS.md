# 06 — Acceptance and Regression Tests

V2.1 完成后，现有测试必须继续通过，并新增以下测试。

## A. Seat Preset

### A1 真人覆盖 AI 预设

1. 房主创建房间。
2. 将 2~6 号配置为 AI。
3. 保存设置。
4. 真人加入 2 号。
5. 结果：成功加入，不返回“座位不可用”。

### A2 开局补 AI

1. 房主配置 2~6 为 AI。
2. 真人加入 2、3。
3. 开始游戏。
4. 结果：4、5、6 自动创建 AI；2、3 保持真人。

---

## B. InformationScope

### B1 死亡狼人不可接收新狼聊

1. A、B 为狼人。
2. A 死亡。
3. 下一晚 B 发送狼队消息。
4. 检查 A 的 owner_view、WebSocket、reconnect snapshot。
5. 结果：均不存在这条新消息。

### B2 普通玩家不可见狼聊

任何阶段普通玩家都不得通过 API、snapshot、WebSocket 或重连恢复得到狼队私聊。

---

## C. Streaming Performance

### C1 Chunk 不写完整状态

模拟 100 个 speech chunks：

- 完整 SQLite save 次数必须显著低于 chunk 数。
- 完整 snapshot 广播次数不得等于 chunk 数。

### C2 发言完成后可恢复

流式完成后重启服务器：

- 完整发言内容仍存在。
- GameState 与发言顺序正确。

---

## D. Revision / Idempotency

### D1 重复投票

同一个 `action_id` 提交两次：

- 只执行一次。
- 第二次返回相同已处理结果或 idempotent acknowledgement。

### D2 过期操作

客户端拿 revision 100 的 vote 请求，在服务器已经 revision 103 后到达：

- 如果该 action 已不合法则拒绝。
- 不得覆盖新状态。

### D3 过期 AI 响应

AI 请求在 turn A 发出；响应返回时已经进入 turn B：

- AI 结果丢弃。
- 状态不变化。
- 记录 stale response。

---

## E. Provider / Fallback

### E1 Key 未配置

未配置 OpenAI Key：

- Provider status 显示未配置。
- UI 不得误导为真实 OpenAI 正常工作。

### E2 Provider 超时

模拟真实 Provider 超时：

- 游戏继续。
- 按配置 fallback 或使用默认合法动作。
- 日志记录原因。

---

## F. Rate Limit / Cost

### F1 Pet Chat 高频请求

短时间连续超过阈值：

- 后续请求被限流。
- 不继续调用第三方 Provider。

### F2 单局预算耗尽

达到 token / 调用次数预算：

- 后续真实模型调用停止。
- 游戏不得崩溃。
- 房主看到清晰状态。

---

## G. Room Lifecycle

### G1 Finished 房间卸载

满足配置的闲置时间后：

- 房间可以从内存移除。
- SQLite 数据仍可按策略保留。

### G2 再来一局

上一局结束后创建新局：

- 新 game_id。
- 不继承旧角色。
- 不泄漏上一局私有身份信息。

---

## H. Mobile

### H1 弱网重连

手机断开网络后恢复：

- 自动 / 手动重连成功。
- 最终 state_revision 与服务端一致。
- 不重复执行之前已提交操作。

### H2 重复点击

快速双击“投票 / 技能”：

- 服务端只执行一次。
- UI 不产生双重反馈。


---

## I. Real-time Chat Rendering

### I1 AI 流式发言即时显示

模拟连续 `speech_chunk`，不发送完整 snapshot：

- 圆桌现场仍持续追加文字。
- chunk 顺序正确。
- `speech_finished` 后完整正文与服务端一致。

### I2 私人搭档消息

发送一条 pet chat：

- 用户消息立即显示。
- 显示回复中状态。
- `private_pet_message` 到达后立即显示 AI 回复。
- 后续 snapshot 不造成重复气泡。

### I3 重连去重

实时 event 已显示后立即断线并通过 snapshot 恢复：

- 事件只显示一次。
- 已完成发言不丢失。

---

## J. TTS / Audio

### J1 开启声音

用户点击“开启声音”后，AI 下一次公开发言：

- 有可听语音。
- 同时继续显示文字。

### J2 静音与回合清理

- 静音后无新语音。
- 回合切换后上一玩家未完成语音被正确清理或停止，不得串到下一座位。

### J3 私有信息保护

默认不得朗读狼人私聊、预言家查验、女巫私有信息或 AI 内部 memory。

---

## K. Model Auto Routing

### K1 首选模型超时

配置两个真实模型，将首选模型强制超时：

- 自动切换第二模型。
- 动作正常完成。
- fallback chain 可观测。
- 不直接进入 Mock。

### K2 全部真实模型失败

所有真实模型失败后：

- 游戏继续。
- 最终使用 Mock / default legal action。
- UI 明确标记 fallback。

### K3 流式中断

模型在已输出部分 speech 后断开：

- 前端保留已输出内容。
- 后备模型不得从头重复整段文本。

---

## L. Agent / Model Isolation

### L1 唯一模型自动分配

4 个 AI 座位、4 个可用 `model_key`：

- 每座位绑定不同 model_key。
- 每座位 agent_id 不同。

### L2 独立模型不足

4 个 AI 座位、2 个可用 model_key、严格模式开启：

- 不得静默复用。
- 开局被阻止并给出明确提示。

### L3 兼容复用仍需上下文隔离

允许模型复用后：

- 两个座位可拥有相同 model_key。
- 但 agent_id、memory、private context 必须不同。
- A 座位无法访问 B 座位隐藏身份和私有信息。
