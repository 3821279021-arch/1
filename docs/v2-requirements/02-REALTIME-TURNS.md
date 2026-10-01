# 实时回合、发言、倒计时与频道

## 核心原则

不要让 AI 在后台瞬间完成整轮。

目标是"实时狼人杀房间"，不是"狼人杀模拟结果生成器"。

## 1. 逐玩家发言

白天示例：

1号发言 → 2号发言 → 3号发言 → 4号发言 → 5号发言 → 6号发言 → 投票。

服务器至少维护：

``` text
phase
current_turn_player_id
turn_started_at
turn_deadline
turn_duration
turn_sequence
```

## 2. 服务器倒计时

倒计时以服务器 deadline 为唯一权威。

前端只能根据服务器时间/deadline
展示剩余时间，不能自己决定回合什么时候结束。

刷新页面后必须恢复原 deadline，不能重新从 30 秒开始。

推荐默认值：

-   白天发言：最大 30 秒
-   遗言：20 秒
-   投票：15 秒
-   狼人讨论：30 秒
-   狼人最终选择：15 秒
-   预言家：15 秒
-   女巫：15 秒

房主以后可以选择：

-   快速：发言约 15 秒
-   标准：发言约 30 秒
-   慢速：发言约 60 秒

## 3. 最大时间，而非强制等待

AI 即使很快生成，也不要瞬间跳过所有人。

AI 发言生成完后可自然停顿约 1～2 秒再切下一位。

不要求 AI 强制等满 30 秒。

真人可以点击"结束发言"提前结束。

## 4. AI 发言展示

公开 AI 发言必须实时显示。

展示 AI 在游戏里"说出口"的内容，不展示模型内部隐藏推理。

若 API 支持 Streaming，WebSocket 使用类似事件：

``` text
speech_started
speech_chunk
speech_chunk
speech_finished
turn_finished
```

界面应能看到 AI 发言逐步出现。

## 5. 真人超时

发言超时：自动结束发言。

投票超时：按房间规则弃票或其他明确规则。

角色技能超时：默认不使用技能。

女巫超时：默认不用药。

狼人超时：如果已有合法多数选择，可采用多数；否则按规则弃权/默认处理。

所有超时由服务器 Rule Engine 执行。

## 6. 公共聊天记录

保留按 Day 分组的公开记录，包括： - 发言； - 系统公告； - 死亡； -
投票结果； - 放逐结果。

用户可向上查看历史。

## 7. 狼队频道

狼人夜间有独立 `🐺 狼队频道`。

狼人可以讨论刀谁。

只有合法狼人视角能收到消息。

其他玩家的客户端和 AI 上下文都不应收到该频道数据。

## 8. AI 宠物私人频道

`🐾 AI 搭档` 是玩家与自己 AI 宠物的私人聊天。

重要：宠物聊天不占用正式游戏回合，也不暂停游戏。

例如 5 号正在发言时，玩家可以问宠物：

"你觉得 5 号怎么样？"

宠物可以根据目前已经发生且主人有权看到的信息实时回答。

## 9. 投票

进入投票阶段后统一倒计时，例如 15 秒。

界面可显示：

``` text
1号 ✓ 已投票
2号 ✓ 已投票
3号 … 思考中
4号 ✓ 已投票
```

投票结束前默认不公开具体目标。

所有人完成或倒计时结束后统一公布票型和放逐结果。

## 10. 推荐 WebSocket 事件

至少考虑：

``` text
phase_changed
turn_started
timer_sync
speech_started
speech_chunk
speech_finished
player_action
turn_finished
vote_started
vote_submitted
vote_result
chat_message
private_pet_message
wolf_chat_message
game_finished
state_snapshot
```

断线重连后服务器发送合法
`state_snapshot`，恢复当前阶段、当前玩家、deadline、聊天记录和可见状态。
