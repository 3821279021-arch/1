# 更新版需求与验收追踪

本次以 `docs/v21-requirements/` 中更新后的 V2.1 需求为准。下面列出已实现的模块和可重复运行的验收证据；最终运行结果、真实模型对局和容器验证见 [VALIDATION.md](VALIDATION.md)。

| 需求 | 实际实现 | 自动化验证 |
|---|---|---|
| R-001 AI 预设不占真人座位 | `game.py` 的 `seat_presets`；`rules.py` 的 `configure/join/leave/start`；大厅保留空座位，开局仅补剩余座位 | `test_v21_core.py`：`test_presets_allow_humans_then_fill_only_remaining_seats`、`test_leaving_lobby_restores_the_ai_seat_preset`、`test_leave_with_live_connection_and_replay_keeps_room_start_and_clock_working` |
| R-002 流式轻量化与完成持久化 | `rooms.py` 的 `actor/commit`；chunk 只发送事件，完成后保存权威状态与完整发言 | `test_v21_core.py`：`test_one_hundred_speech_chunks_only_send_events_then_persist_completion`：100 段期间零次完整保存、零次 snapshot；完成一次保存、一次 snapshot，SQLite 恢复正文和下一发言人 |
| R-003 死亡狼人隔离 | `rules.py` 的 `kill_player` 保存死亡时可见边界与队友状态；`scope.py` 过滤后续狼聊；`rooms.py` 按成员权限逐连接发送 | `test_v21_core.py`：`test_dead_wolf_cannot_receive_new_private_events_or_reconnect_history`、`test_nonwolf_never_receives_wolf_chat_or_other_pet_events`；另有夜间执行状态不暴露技能持有者的回归测试 |
| R-004 限流与预算 | `limits.py`；`main.py` 的 session API；`rooms.py` 的创建、pet、开局入口；`llm.py` 每次真实 HTTP 尝试预留/结算；`persistence.py` 的 usage 状态 | `test_limits.py` 覆盖速率、并发预留、重试计数、token/费用、每日切换、重启和存储故障；`test_api_v22.py` 检查 HTTP 429；`test_v21_core.py` 检查 pet 高频不再调度 AI、零请求预算不发 HTTP 且继续合法游戏 |
| R-005 狼人 AI 真实讨论 | `rooms.py` 的 `discussion_rounds` 串行两轮；`ai.py` 的 `wolf_discuss` 使用合法局面、先前狼聊与本人记忆；最终夜杀另经 `RuleEngine.apply` | `test_v21_core.py`：`test_wolf_discussion_is_two_rounds_with_legal_sequential_context`：第二狼看到第一条、第二轮看到第二条；不含其他角色查验/记忆；讨论不修改夜杀，正式动作才写入选择 |
| R-006 增量结构化记忆 | `memory.py` 的 `update_memory`；`rules.py` 在合法事件生成时更新每位玩家独立 memory；`scope.py` 和 `ai.py` 提供短期原始事件及长期结构化证据 | `test_memory.py`：早期证据、角色/查验声明的来源、矛盾/立场变化、票型、私有查验、去重、旧保存迁移与提示词隔离 |
| R-007 实际模型及降级可见 | `model_registry.py` 的配置/健康状态；`llm.py` 的 execution、safe records；`rooms.py` 的 scoped `model_execution`；大厅与座位显示实际模型和 fallback | `test_model_routing.py`：`test_provider_diversity_configuration_and_no_secrets`、安全日志和故障路由；`test_api_v22.py` 检查无 Key 泄漏；`browser_v21.py` 检查未配置模型被禁用、兼容复用提示 |
| R-008 revision、幂等和过期保护 | `game.py` 的 `state_revision/processed_actions`；`rules.py` 在事件产生时固定 revision/event ID；`rooms.py` 的命令缓存和 `applicable`；前端同 ID 重试、拒绝旧状态 | `test_v21_core.py`：动作只执行一次并可重启重放、旧请求不变更状态、旧回合 AI 被丢弃并记录 `stale_ai_response`；`test_api_v22.py`：创建/关闭重复请求；`browser_v21.py`：重复点击、断线重试保留 ID、迟到上一局 snapshot 不覆盖新局 |
| R-009 房间生命周期 | `game.py` 的生命周期字段；`rooms.py` 的维护、只扫描 ACTIVE、非 ACTIVE 卸载/按需读取、关闭取消任务；`persistence.py` 的归档与 identity 期限；`rules.py` 的 rematch | `test_v21_core.py`：完成房间卸载后 SQLite 数据保留并可读取、关闭取消 AI、再来一局保留真人和新建 game ID/私有状态；`test_api_v22.py`：关闭后无待操作动作 |
| R-010 Mobile/PWA | `static/app.js` 的本人回合提示、连接状态、pending、Wake Lock、安装提示及可选震动；`app.css` 的触控区域和 Safe Area；Service Worker | `browser_v21.py`：360/390/768/1280 宽度不溢出、按钮 pending、网络状态及同 ID 重试；`browser_smoke.py`：真实本地服务器的多人浏览器对局与刷新恢复。Wake Lock/安装入口按浏览器能力降级 |
| R-011 实时文字与聊天 | `static/app.js` 显式处理 speech、public chat、pet、wolf、phase 和 reconnect 事件；event ID 去重；pet 用户气泡立即显示；部分输出保留并提示错误 | `browser_v21.py`：无全量 snapshot 的 chunk 在 300ms 内显示、chunk 去重、pet 乐观气泡/回复状态、后续 snapshot 不重复、断线恢复保留完成记录；`test_v21_core.py` 验证事件顺序与持久化 |
| R-012 公开发言 TTS | `static/app.js` 的用户点击启用声音、中文 voice 优先、按完整句子排队、每座位 voice profile、独立 pet 朗读开关及静音/停止/回合切换清理 | `browser_v21.py` 用 `speechSynthesis` stub 验证 `zh-CN`、不读零碎 chunk、不同座位音高、静音及清队列、私有消息默认不读。真实设备可听效果仍需手机浏览器人工确认，不能用 stub 证明扬声器播放 |
| R-013 模型池及自动切换 | `model_registry.py` 精确 `provider:model` 注册及能力/健康状态；`llm.py` 首选、可配置重试、其他真实候选、最后 Mock；已有部分流式正文时结束并保留，不重播整段 | `test_model_routing.py`：首选超时转第二实际模型且 prompt 相同、非法格式切换、全部失败明确 Mock、首字前切换、已输出部分不重复、取消及 partial 用量结算 |
| R-014 独立 Agent/唯一模型分配 | `Player/PetAI` 独立 agent ID、memory、conversation state；`model_registry.py` 的 `allocate` 优先保留锁定模型，再分配未使用实际模型；默认严格模式，兼容模式显示共用座位 | `test_model_routing.py`：4 模型唯一分配、锁定保留、严格不足阻止开局、明确兼容、同 Provider 并发模型不串上下文；`test_api_v22.py`：模型不足不启动、不调用；`test_memory.py` 和 core 测试验证私有信息隔离 |

Mock 是大厅明确标出的练习模式，不作为真实模型池中的不同模型数量。仅配置一个 API Key 并不等于拥有足够不同实际模型；严格模式不足时必须增加可用模型，或由房主明确选择兼容复用/Mock 练习。

## Acceptance A–L

| 验收组 | 对应测试与范围 |
|---|---|
| A1–A2 | core 的预设加入和开局补位，另含离座后回填和已连接离座 |
| B1–B2 | core 的死亡狼人 owner、连接增量、重连 snapshot、队友状态冻结，以及普通玩家狼聊/他人 pet 隔离 |
| C1–C2 | core 的 100 chunk 零完整保存/广播及发言完成后 SQLite 恢复 |
| D1–D3 | core 的动作 UUID 重放、过期 revision 不变更状态、旧 turn AI 丢弃及日志；浏览器处理迟到事件和 snapshot |
| E1–E2 | registry/API/浏览器诚实显示配置状态；router 模拟超时、回退和安全日志 |
| F1–F2 | manager 的 pet 调度计数与零真实预算 HTTP 阻止测试；limits 的硬额度并发、每日和重启测试 |
| G1–G2 | core 的非 ACTIVE 卸载后按需恢复及新 game ID/私有信息清空 |
| H1–H2 | browser 的模拟弱网重连、pending、双击只发一次、重试同 ID；core/API 的服务端幂等；实际移动网络行为需设备补验 |
| I1–I3 | browser 的事件直接显示、即时 pet 气泡、回复事件及 snapshot 去重、恢复完整记录 |
| J1–J3 | browser 的中文 TTS 调用/句子队列、切回合取消、静音、默认私有内容不朗读；J1 可听效果保留为真实设备检查 |
| K1–K3 | router 的第二实际模型故障切换、全失败 Mock、部分流保留且不重播；浏览器保留中断正文 |
| L1–L3 | registry 的唯一分配/严格不足/显式兼容，模型并发隔离、独立 agent ID 及 memory/context 私有范围测试 |

## 重现方式

在项目目录运行 `python -m unittest discover -s tests -v`。

浏览器测试需先安装开发依赖及 Chromium（`pip install -r requirements-dev.txt`、`python -m playwright install chromium`），用 `python run.py` 启动本地服务，再在另一个终端运行：

```bash
TEST_BASE_URL=http://127.0.0.1:8000 python tests/browser_v21.py
```

浏览器验收脚本使用受控 WebSocket/API 与 TTS stub，适合复现重连、迟到事件和故障条件。真实模型对局脚本 `tests/live_dashscope.py` 是单独的显式联网验证，读取服务端环境中的凭据，不把 Key 写入交付包。
