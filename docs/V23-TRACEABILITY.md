# V2.3 要求对照

原建议完整保存在 `v23-requirements/`，以下只描述本版本实现与验证范围。

| 里程碑 | 实现位置 | 验证与限制 |
| --- | --- | --- |
| A：Token/Memory | tokens.py、tokenizers/、memory.py、prompt_budget.py、ai.py、llm.py | 所有列表上限、来源摘要、每日摘要、衰减、复杂摘要预算裁剪；30 天合成对比与 22 天规则长局。DashScope 真实 Usage 估算误差中位数约 15%，含安全余量，详见 V2.3.1 实测。 |
| B：模型层 | providers.py、llm.py、main.py | 共享 Client、四家统一结果/Usage、原生 schema/tool 或 JSON 模式、本地格式/合法性、默认一次修复、重试预算与错误类型。四家协议使用 MockTransport 回归；DashScope 五个实际模型完成 5 局分批实测及真实后备、浏览器私聊。 |
| C：策略层 | strategy.py、ai.py、rooms.py | 可打印/可测试信念状态，声明/票型/矛盾评分；默认服务器动作与语言表达分开；狼队计划、公开表达信息隔离；自然表述记录低置信度来源。尚无实战胜率承诺。 |
| D：多人 | security.py、persistence.py、rules.py、rooms.py、main.py、static/js/ | 踢人、锁房、密码、重入权限、恢复码、轮换/撤销、房间/会话限流、重连权威快照及幂等。API 与真实浏览器回归通过；锁屏/蜂窝网络须手机实测。 |
| E：工程 | persistence.py、static/js/、requirements*.lock、.github/workflows/ci.yml | 独立语义事件/记忆/会话/成本边界，线程化 IO，实际清理，模块职责与网络入口分离，锁定依赖，CI 与 Docker smoke。继续使用一个 worker/实例；未迁移 PG/Redis。 |

V2.4 的扩展人数、角色、观战、完整回放 UI、排行、成长、好友未加入本版，与建议路线图一致。

## API 增量

| 路径 | 方法 | 作用 |
| --- | --- | --- |
| /api/session/recover | POST | 无需原 Token，输入恢复码，恢复原 owner_id 并轮换 Token/恢复码 |
| /api/session/recovery-code | POST | 原身份重新生成恢复码，旧码失效 |
| /api/session/rotate | POST | 替换 Token，关闭旧设备连接 |
| /api/session/revoke | POST | 主动撤销 Token、断开连接，保留到期前恢复能力 |
| /api/rooms/{id}/lock | POST | 大厅房主设置 locked |
| /api/rooms/{id}/password | POST | 大厅房主设置密码，空字符串移除 |
| /api/rooms/{id}/kick | POST | 大厅房主移除指定 seat 并禁止原 owner 重入 |
| /api/rooms/{id}/reopen | POST | 大厅房主解除指定 seat 的原玩家重入限制 |
| /api/rooms/{id}/cost-report | GET | 房主获取报告；未结束仅预算汇总，结束后详细成本 |

既有 join 请求增加可选 password。WebSocket 第一帧增加可选 last_event_id；返回 sync.mode=authoritative_snapshot。未授权用户不能读取其他玩家快照、成本报告或私有记忆。
