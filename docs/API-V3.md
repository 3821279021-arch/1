# V3 API 简表

除首页、静态资源、health 和创建/恢复会话外，使用 `Authorization: Bearer <session token>`。WebSocket 通过首帧认证，不把 token 放在 URL。

| API | 用途 |
| --- | --- |
| POST /api/session | 创建会话并发身份恢复码 |
| POST /api/session/recover | 用恢复码恢复身份并替换旧 token |
| POST /api/rooms | 创建房间；name、mode、pace，可选 seat；custom 传 player_count/roles |
| POST /api/rooms/{id}/join | 加入，name、password 可选；省略 seat 随机分配 |
| GET /api/rooms/{id} | 按本人合法视角读取 snapshot |
| POST /api/rooms/{id}/seat | 大厅换座；seat 为指定座位或省略随机换座 |
| POST /api/rooms/{id}/configure | 房主板子/节奏/AI 座位模型配置 |
| POST /api/rooms/{id}/start | 开始，自动补齐空座 AI |
| POST /api/rooms/{id}/action | 本回合动作；game_id/turn_id/turn_sequence/action及对应字段 |
| POST /api/rooms/{id}/wolf-chat | 狼频道合法私聊 |
| PATCH /api/rooms/{id}/pet | 咨询/托管及搭档设置 |
| POST /api/rooms/{id}/rematch | 保存已完成局并在同房间新开一局 |
| GET /api/rooms/{id}/analysis?game_id=... | 已完成局身份、指标、公共回放；game_id可省略 |
| GET /api/rooms/{id}/replay?game_id=... | 已完成局公共事件 |
| GET /api/rooms/{id}/games | 此房间已完成局目录 |
| GET /api/credentials | 本人的脱敏凭据列表 |
| POST /api/credentials | provider、api_key、base_url?、temporary?、scope_id? |
| GET /api/credentials/{id} | 脱敏详情 |
| PUT /api/credentials/{id} | api_key、base_url?，修改后递增 revision |
| DELETE /api/credentials/{id} | 撤销并取消旧任务 |
| POST /api/credentials/{id}/models | 动态账号模型目录，失败允许手动 ID |
| POST /api/credentials/{id}/test | 测试目录；body可含 model_id 用最小推理兜底验证 |
| GET /api/models | 平台已配置模型目录 |
| POST /api/models/refresh | 刷新平台账号目录 |
| GET /api/health | 版本、脱敏供应商状态、角色风格、模式定义 |

凭据操作可附 `?scope_id=room_id`。临时凭据不传 scope_id 则绑定当前身份会话；传 room_id 则只供该房间使用，服务重启后消失。

模式为 `quick6`、`standard9`、`standard12` 或 `custom`。自定义 roles 是角色 key 数组，每个角色一项：wolf/seer/witch/villager/hunter/guard/knight/idiot/wolf_king/white_wolf_king/wolf_beauty/hidden_wolf。

AI 座位项示例：

```json
{
  "id": 3,
  "credential_id": "用户拥有的凭据ID",
  "model_id": "供应商模型ID",
  "personality": "detective",
  "model_options": {"temperature": 0.8}
}
```

后台从登录身份校验凭据所有权，忽略客户端伪造的 credential_owner_id。`model_options` 只接受白名单字段：temperature、reasoning_effort、thinking_budget、enable_thinking、max_output_tokens。供应商和模型是否支持参数以其协议为准。

action 支持 speech、vote、wolf_discuss、wolf_kill、seer_inspect、witch、guard_protect、hunter_shoot、wolf_king_shoot、wolf_beauty_charm、duel、self_destruct。合法类型、目标和药剂条件以 snapshot.pending_action/secondary_actions 为准。target null 表示弃权；女巫用 save 与 poison_target；公开发言用 speech；结束讨论可带 text。

每条可重试命令建议传唯一 action_id，同一动作 ID 幂等回放。携带旧 game_id/turn_sequence/turn_id 的动作会被拒绝。回放返回按时间顺序的公开事件 envelope，其文字和阶段字段在 `data` 内。

统计是赛后可核对指标，不是能力排名。公开身份自称和查验自称仍标记为未确认声明；判断变化从公开文本提取，有事件出处，不回灌本局模型。未知价格成本为 null，Mock 不计真实调用费用。
