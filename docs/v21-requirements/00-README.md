# AI Werewolf V2.1 Requirements

本包是在现有 AI Werewolf V2 源码基础上的 V2.1 改进需求，目标不是重写架构，而是修复多人实战、长时间运行、真实大模型接入后会暴露的关键问题。

## V2.1 目标

1. 修复大厅 AI 座位预设导致真人无法加入的问题。
2. 降低 AI 流式发言期间的数据库写入与 WebSocket 全量广播压力。
3. 修复死亡狼人继续接收狼队频道的隐藏信息泄露问题。
4. 增加公网部署所需的限流、预算与滥用保护。
5. 将 AI 狼人讨论升级为真实的大模型协作，而不是固定模板。
6. 将 AI Memory 从“最近事件扫描”升级为结构化、增量式推理记忆。
7. 明确展示真实模型 / Mock / fallback 状态，避免用户误判当前运行模型。
8. 增强 WebSocket 与动作协议的版本控制、幂等与弱网恢复能力。
9. 增加房间生命周期、归档、清理和内存卸载机制。
10. 改善手机端轮到玩家操作、息屏、PWA 安装等体验。
11. 修复 AI 流式聊天/搭档聊天不能即时显示的问题。
12. 增加 AI 公开发言语音/TTS。
13. 增加真实模型池与自动故障切换。
14. 每个 AI 座位使用独立 Agent，并默认禁止同一实际模型在同一局重复扮演多个 AI 角色。

## 优先级

### P0 / 必须优先修复

- R-001 AI 座位预设不可锁死真人座位
- R-002 AI 流式发言不得频繁整局落库和全量广播
- R-003 死亡狼人不得继续接收新的狼队私有信息
- R-004 公网模型调用必须具备限流和费用保护
- R-011 实时聊天与流式发言必须即时显示
- R-013 模型池与自动故障切换
- R-014 独立 Agent 与唯一模型分配

### P1 / 本版本重点

- R-005 狼人 AI 真实讨论
- R-006 增量结构化 AI Memory
- R-007 Provider / fallback 可见性
- R-008 state_revision、action_id、过期操作保护
- R-009 房间生命周期管理
- R-012 AI 发言语音 / TTS

### P2 / 体验优化

- R-010 Mobile/PWA 交互增强

## 文件说明

- `01-CORE-FIXES.md`：P0 核心修复
- `02-REALTIME-AND-PERSISTENCE.md`：实时协议、写库与弱网一致性
- `03-AI-ORCHESTRATION-AND-MEMORY.md`：AI 狼队讨论、模型输出与记忆
- `04-SECURITY-COST-AND-LIFECYCLE.md`：安全、费用保护和房间生命周期
- `05-MOBILE-PWA-UX.md`：手机端体验要求
- `06-ACCEPTANCE-TESTS.md`：验收与回归测试
- `07-TRACEABILITY.md`：需求与验收映射
- `08-AUDIO-CHAT-MODEL-ROUTING.md`：语音、实时聊天、模型池与独立 Agent

## 非目标

V2.1 不要求：

- 重写现有 RuleEngine
- 更换 FastAPI / SQLite 技术栈
- 开发原生 iOS / Android App
- 增加大量新角色或新板子
- 推翻现有 InformationScope 架构

现有核心架构应尽量保留，以最小改动完成可靠性升级。
