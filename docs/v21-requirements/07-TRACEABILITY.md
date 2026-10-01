# 07 — Requirement Traceability

| ID | 需求 | 优先级 | 主要模块 | 验收测试 |
|---|---|---:|---|---|
| R-001 | AI 座位预设不锁死真人 | P0 | Room / Lobby / Player | A1, A2 |
| R-002 | 流式发言轻量化 | P0 | WebSocket / Persistence | C1, C2 |
| R-003 | 死亡狼人隔离后续狼聊 | P0 | InformationScope | B1, B2 |
| R-004 | 限流与费用保护 | P0 | API / AI Orchestrator | F1, F2 |
| R-005 | 狼人 AI 真实讨论 | P1 | AI Orchestrator | 建议新增行为测试 |
| R-006 | 增量结构化 AI Memory | P1 | Memory | 建议新增长局一致性测试 |
| R-007 | Provider/fallback 可见 | P1 | Provider / UI | E1, E2 |
| R-008 | revision + 幂等 + 过期保护 | P1 | API / WebSocket / RuleEngine | D1-D3 |
| R-009 | 房间生命周期 | P1 | RoomManager / Persistence | G1, G2 |
| R-010 | 手机/PWA 体验增强 | P2 | Frontend / PWA | H1, H2 |

## 推荐实施顺序

```text
R-001
  ↓
R-002 + R-003
  ↓
R-008
  ↓
R-004 + R-007
  ↓
R-005 + R-006
  ↓
R-009
  ↓
R-010
```

## Definition of Done

V2.1 可认为完成，当且仅当：

- 所有 P0 需求实现并有自动化回归测试。
- 现有 V2 测试全部继续通过。
- 多人手机局不会因 AI 座位预设阻塞真人加入。
- 流式 AI 发言不会按 chunk 整局写库/全量广播。
- 死亡狼人无法获得死亡后的狼队信息。
- 公开部署具备最基本限流和模型预算保护。
- 过期操作、重复操作、过期 AI 响应不会改变当前权威状态。
- UI 能准确显示 Provider 是否真实可用或已 fallback。


| R-011 | 实时聊天与流式发言即时显示 | 08 | I1-I3 | P0 |
| R-012 | AI 公开发言 TTS / Audio | 08 | J1-J3 | P1 |
| R-013 | 模型池与自动故障切换 | 08 | K1-K3 | P0 |
| R-014 | 独立 Agent 与唯一模型分配 | 08 | L1-L3 | P0 |
