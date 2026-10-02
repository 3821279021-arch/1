# V3.2.0 产品体验 API

所有新接口复用 Bearer 会话身份。V3.1 的动作、BYOK、research export、trace schema、Arena CLI 和公开回放仍兼容；不引入联赛 API。

| 接口 | 行为 |
| --- | --- |
| `POST /api/rooms/{id}/pause` | 仅房主；ACTIVE → SUSPENDED，保存剩余倒计时并取消房间 AI 与私人商议任务 |
| `POST /api/rooms/{id}/resume` | 原参与者显式继续；同一个 turn_id、turn_sequence 与剩余时间，重连不会自动继续 |
| `POST /api/rooms/{id}/leave` | 明确退出并断开该身份的连接；最后一名在线玩家退出立即暂停，保留原座位以便继续 |
| `DELETE /api/rooms/{id}` | 仅房主，关闭并删除快照、私有记忆、聊天、事件、模型记录与历史对局，仅保留匿名统计 |
| `GET /api/models/preferences` | 本身份的 favorites、recent、lineups；recent 只有实际调用的分类状态与 Unix 时间 |
| `PUT /api/models/preferences` | 部分更新 favorites / lineups；拒绝其他字段。最多 200 收藏、30 阵容；阵容保存 id/name/player_count/seats，座位仅 id/model_key/personality |
| `GET /api/history` | 本身份参与的最近 100 场已完成对局：时间、角色、胜负、模型与同桌昵称 |
| `GET /api/history/{room}/{game}` | 原参与者的已完成对局分析和公开事件；重开后仍可访问，删除/TTL 后为 404 |

收藏以平台 `provider:model` 或 BYOK `credential:id:model` 标识保存，不保存密钥。recent 状态为 success/no_permission/quota_exhausted/rate_limited/not_found/error；未调用显示「未测试」，不查询实时余额。会话恢复保留偏好，其他身份不可读取。

创建/配置新增可选字段（旧客户端省略时仍为 fixed）：

```json
{"board_policy":"custom_random","player_count":9,"random_role_pool":["wolf","seer","witch","hunter","villager"]}
```

board_policy 为 fixed/constrained_random/custom_random。人数 4～16，自定义池必须能满足阵营与角色标签约束，否则 400，不偷偷回退。新增固定 mode 为 wolfking12、beautyknight12、hidden12、advanced9、fun6、special12。

随机局大厅/进行中：role_roster 和 game_mode.roles 为空；rules.roles 是**可能角色池**，board_framework 是数量约束；夜间 phase/phase_name 泛化为 night/夜间行动。self/pending_action 仍只含本人合法信息，phase_changed 同样过滤。结束后实际身份公开。

快照兼容新增 lifecycle=SUSPENDED、suspended_remaining、board_policy、random_role_pool、board_framework，以及公开行动玩家的 thinking/thinking_started_at。房间事件新增 room_suspended/room_resumed/room_left；权威 state_snapshot 决定页面状态。旧客户端忽略新增字段后仍能读取普通固定局。

真实流式调用记录增加 first_token_ms、generation_ms、output_characters，原 provider/model/usage 不变。分析展示首 token 均值、生成总时长和字数；UI 缓冲不进入统计。无实际调用为 — 或零，不将 Mock 数据伪装成真实 Provider 结果。

数据库仅增加 model_preferences 与 model_last_calls；通过 CREATE TABLE IF NOT EXISTS 增量兼容，无破坏迁移。暂停、板子与 TTL 字段放在原 JSON 快照中，旧存档使用默认值。已删除房间的晚到取消记录会被匿名统计标记拦截，不复活私有输出。
