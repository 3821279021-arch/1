# 08 — Audio, Real-time Chat, Model Routing and Agent Isolation

本文件补充 V2.1 在实际试玩中发现的四项关键问题：AI 发言无声音、实时聊天输出不显示、Provider 不能自动切换、同一模型同时扮演多个角色。

## R-011 实时聊天与流式发言必须即时显示（P0）

### 当前问题

服务端已经产生 `speech_started`、`speech_chunk`、`speech_finished`、`chat_message`、`private_pet_message`、`wolf_chat_message` 等事件，但前端 WebSocket 不能只依赖 `state_snapshot` 来刷新页面。

### 要求

1. 前端必须显式处理以下 WebSocket 事件：
   - `speech_started`
   - `speech_chunk`
   - `speech_finished`
   - `chat_message`
   - `private_pet_message`
   - `wolf_chat_message`
   - `phase_changed`
   - `reconnect`
2. `speech_chunk` 到达时必须立即追加到“圆桌现场”的当前发言文本，不等待完整 snapshot。
3. `chat_message` 必须立即追加公共记录。
4. `private_pet_message` 必须立即追加到 AI 搭档聊天窗口。
5. `wolf_chat_message` 仅对有权限的存活狼人实时追加。
6. `state_snapshot` 仍然是权威状态，用于首次加载、重连和纠偏，而不是实时文字显示的唯一方式。
7. 所有消息都必须带稳定的 `event_id` 或单调递增 `seq`，客户端按 ID 去重，防止 snapshot 与实时 event 重复显示。
8. 用户发送 AI 搭档消息后，应立即显示自己的气泡，并显示“正在回复”状态；收到回复事件后替换加载状态。
9. AI 流式发言失败时，已输出文字保留，并显示非阻断错误状态，不得整段消失。

### 验收标准

- AI 每输出一个可见 chunk，手机端 300ms 内可见（不计第三方模型网络延迟）。
- 不依赖完整 state snapshot 也能连续显示一整段流式发言。
- 重连后无重复消息、无丢失已完成发言。

---

## R-012 AI 发言语音 / TTS（P1）

### 当前问题

现有前端没有 TTS、`speechSynthesis` 或音频播放实现，因此 AI 发言只有文字。

### 基线实现

1. 浏览器端优先使用 Web Speech API `window.speechSynthesis` 实现免后端成本 TTS。
2. 页面提供明确的“开启声音 / 静音”开关，默认遵守浏览器自动播放规则。
3. 第一次开启声音必须来自用户点击，以满足 iOS / Android 浏览器的音频权限要求。
4. 默认朗读语言为 `zh-CN`，无中文 voice 时选择可用 voice 并保持文字正常显示。
5. 只朗读公开 AI 玩家发言；系统消息、隐藏身份、狼人私聊、私人记忆默认不得朗读。
6. AI 搭档私聊可提供独立“朗读搭档回复”开关，默认关闭。
7. 每个 AI 座位必须有独立 `voice_profile`：
   - voice / voice_id
   - rate
   - pitch
   - optional volume
8. 不同 AI 座位应尽量分配不同声音或不同 pitch/rate，使玩家能通过声音区分角色。
9. 流式文本按完整句子或标点缓冲后进入 TTS 队列，避免每 6 个字单独朗读。
10. 回合切换、玩家死亡、退出房间或重连时必须正确结束/清理上一段语音队列，避免串台。
11. 提供“停止朗读”能力。
12. 浏览器不支持 TTS 时退化为纯文字，不影响游戏。

### 可选增强

可在后续支持云 TTS，但不得作为 V2.1 基础可玩性的硬依赖。

### 验收标准

- 用户点击“开启声音”后，下一位 AI 的公开发言可听到中文朗读。
- 2 号与 3 号 AI 连续发言时不会声音内容串到错误座位。
- 快速切换回合不会重复朗读上一句。
- 静音后不再播放，但文字继续实时显示。

---

## R-013 模型池与自动故障切换（P0）

### 当前问题

目前指定 Provider 调用失败后只重试并降级到 Mock，不能自动在 OpenAI / Anthropic / Gemini 或同一 Provider 的其他模型之间切换。

### Model Registry

系统必须引入模型注册表，模型的唯一键必须精确到实际模型，而不是只有 provider：

```json
{
  "key": "openai:gpt-x",
  "provider": "openai",
  "model": "gpt-x",
  "configured": true,
  "enabled": true,
  "healthy": true,
  "capabilities": ["chat", "json", "stream"]
}
```

必须允许同一 Provider 注册多个实际 model ID。

### 自动路由

每次 AI 调用必须支持以下路由策略：

1. 首选当前 Agent 绑定模型。
2. 可配置地重试当前模型一次。
3. 当前模型持续失败、超时、限流或返回无效格式时，从健康的候选模型池选择下一模型。
4. 依次尝试其他已配置真实模型。
5. 所有真实模型不可用时才进入 Mock / rule fallback。
6. 自动切换不得突破 InformationScope；切换后看到的上下文必须与原 Agent 完全相同。
7. 必须记录：
   - requested_model
   - model_used
   - provider_used
   - fallback_chain
   - failure_reason
   - latency
8. UI 必须能看到当前实际使用的模型以及“已切换 / fallback”状态。

### Streaming 特殊规则

- 如果流式发言在输出任何文字前失败，可透明切换到下一个模型重新开始。
- 如果已经输出部分文字后失败，不得简单从头重播造成重复内容；应结束当前句或让后备模型基于已输出文字继续，且必须去重。

### 验收标准

配置至少两个真实模型后：

- 人为让首选模型返回超时，AI 能自动用第二模型继续完成合法动作。
- UI 与日志显示真实使用的第二模型。
- 只有全部真实模型失败后才出现 Mock。

---

## R-014 每个 AI 座位必须是独立 Agent，默认禁止同一实际模型重复扮演多个角色（P0）

### 当前问题

当前 `Player` 只有 `provider`，多个座位可以同时绑定同一个 Provider / model，并且没有明确的 Agent 隔离标识。

### Agent 数据模型

每个 AI 座位必须拥有独立 Agent 配置：

```json
{
  "agent_id": "uuid",
  "seat_id": 2,
  "provider": "openai",
  "model": "gpt-x",
  "model_key": "openai:gpt-x",
  "personality": "detective",
  "voice_profile": {},
  "memory": {},
  "conversation_state": {}
}
```

### 强隔离要求

1. `agent_id` 必须与座位一一对应，整局中不可被另一个座位复用。
2. 每个 Agent 的 memory、private context、角色身份和消息历史完全独立。
3. 不允许把一个座位的完整 prompt/history 继续作为另一个座位的会话上下文。
4. 默认启用 `unique_model_per_ai_seat=true`：同一个 `model_key` 在同一房间中只能绑定一个 AI 座位。
5. 分配模型时先使用未占用的健康模型，直到模型池耗尽。
6. 如果 AI 座位数大于可用唯一模型数：
   - 严格模式：阻止开始游戏，并提示“可用独立模型不足”。
   - 兼容模式：允许复用，但 UI 必须明确提示哪些座位共用模型；不得静默复用。
7. 即使兼容模式复用了相同底层 model，也仍必须使用独立 Agent、独立 memory、独立上下文，防止身份串线。
8. 房主可以手动锁定某个座位的模型，也可以选择“自动分配”。
9. 自动分配应优先多样化 Provider 与 model，而不是连续给多个座位同一个模型。
10. 游戏开始后，模型故障切换不得把两个存活 Agent 永久合并成一个会话；只是更换其 `model_key`。

### UI 要求

大厅每个 AI 座位显示：

- Agent 名称 / 性格
- Provider
- 实际 model 名称
- 自动 / 锁定
- 当前模型健康状态

游戏中座位详情可以显示公共的模型标签，但不得显示任何隐藏身份相关信息。

### 验收标准

- 4 个 AI、4 个可用实际模型时，4 个 AI 必须自动获得 4 个不同 `model_key`。
- 只有 2 个模型且严格模式开启时，4 AI 游戏不得静默启动。
- 即使启用兼容复用，两个使用同底层模型的座位也必须拥有不同 `agent_id`、memory 和私有上下文。
- 任何测试中不得发生 A 座位读取 B 座位的角色身份、私有查验或内部 memory。
