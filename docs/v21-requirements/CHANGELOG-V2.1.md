# V2.1 Proposed Changelog

## Fixed

- 修复 AI 座位预设提前占座导致真人无法加入的问题。
- 修复死亡狼人继续收到未来狼队私聊的问题。
- 修复 AI 流式文本导致高频 SQLite 整局写入和全量状态广播的问题。
- 增加旧 turn / 旧 revision AI 响应保护。

## Added

- `state_revision`
- `action_id` 幂等语义
- AI provider availability / fallback 状态
- 模型调用限流与预算保护
- 房间归档 / 卸载 / 清理生命周期
- 真实狼人 AI 战术讨论
- 增量结构化 AI Memory
- 手机端轮到操作提醒、重连状态、Wake Lock / PWA 体验建议

## Architecture Principle

继续保持：

```text
Client
  -> Session / Room Manager
  -> RuleEngine / State Machine
  -> InformationScope
  -> AI Orchestrator
  -> Persistence / Event Log
```

AI 永远只提出 proposal，RuleEngine 永远拥有最终状态修改权。


## 2026-09-30 — Playtest Addendum

新增实际试玩中发现的要求：

- R-011：WebSocket 实时聊天/流式发言直接渲染，不再只依赖 snapshot。
- R-012：公开 AI 发言 TTS、声音开关与每座位 voice profile。
- R-013：真实模型池、健康状态、跨模型自动 fallback。
- R-014：每座位独立 Agent；默认同局禁止重复使用同一实际 model_key。

新增 Acceptance I-L，覆盖聊天显示、TTS、自动换模型和 Agent 隔离。
