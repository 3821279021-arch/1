# V3 需求与实现映射

本表对应交付包内原始需求 `docs/v3-requirements/`。实际验收证据见 `V3-ACCEPTANCE.md`。

| 需求 | 实现 | 验证 |
| --- | --- | --- |
| 左右玩家头像、中央实时对话、状态常驻 | static/index.html、app.css、js/game.js；历史和设置抽屉 | browser_v3.py 桌面/移动布局 |
| 真正声音、首次解锁、独立 TTS 与可见错误 | js/audio.js、本地 WAV 资源 | 浏览器点击播放及拒绝播放反馈；实际设备听音另列 |
| 创建/加入随机入座，可选换座 | rules.join/change_seat、POST /rooms/{id}/seat | test_v3_rules.py、test_v3_api.py |
| 完成即推进，未操作者超时兜底，空阶段跳过 | rules.required_actors/completion/tick、rooms.actor | 未到期 HTTP 全员提交与完整局 |
| 模型自主判断、欺骗、诈身份与变更立场 | ai.context/propose/speak/choose_secondary，仅合法视角 | test_v3_ai.py 模型不同票、公开谎报/隐瞒、旧 server 变量无效 |
| 用户 BYOK、加密保存、掩码、替换/删除/临时 key | credentials.py、main 凭据API、模型中心 | 凭据专测、跨用户API、SQLite检查、restart、422隐私 |
| 动态模型目录、原生供应商与兼容接口、手动 model ID | credentials.discover/test、providers.py、llm.request_scope | 五供应商模拟传输、动态目录、隔离与错误摘要 |
| 每个 AI 座位不同模型 | rooms.configure/start、Player 凭据路由 | 分配校验/独立模型/私有路由 |
| 6/9/12 与自定义人数 | roles.GAME_MODES/validate_mode、动态前端布局 | 完整 Mock 局与布局 |
| 更多角色及模块化 | roles.RoleDefinition 技能/目标/死亡钩子 | 猎人/守卫/骑士/白痴/狼系技能测试 |
| 赛后统计与回放 | completed_games、public_replay、rooms.analysis | 未结束隔离、统计、重赛旧局、持久化 |
| 长期模型榜单 | 需求明确为未来可选，不发布单局能力榜 | 本版本提供逐局数据与回放 |

## 关键设计边界

合法性与策略分开：模型输入不含平台怀疑概率、指定目标、expression plan，也不含凭据。角色本人可见信息由统一 InformationScope 提供；记忆保留事实来源，不提前把发言自称当真实身份。

夜间技能的执行身份、详细遥测与行动历史不出现在公共实时状态；完成后参与者可查看竞技统计。重赛前的完整对局快照单独保存，不与新 game_id 混合。

Mock 练习与故障兜底会显示模拟身份，不冒充真实模型能力结果。真实上游调用和设备听音未经本次可用凭证/设备验证的部分在验收报告明确列出。
