# 03 — AI Orchestration and Memory

## R-005 狼人 AI 必须支持真实协作讨论

### 问题

现阶段狼人讨论若使用固定模板或简单规则选目标，玩家会看到“像在讨论”，但 AI 实际没有根据局势交流战术。

### 目标

狼队夜间讨论应成为真正的 AI-to-AI 协作流程，同时保持严格的信息权限。

### 输入

狼人 AI 讨论上下文只能包含：

- 自己身份
- 当前存活狼队友
- 当前合法公开信息
- 自己过去合法获得的信息
- 狼队历史私聊（仅其存活期间合法可见部分）
- 自己的结构化 memory

禁止包含：

- 其他隐藏角色真实身份
- 预言家私有验人结果（除非公开发言已经透露）
- 女巫私有信息
- 服务端完整 GameState

### 行为

狼人 AI 可以讨论：

- 今晚刀谁
- 是否冲锋 / 倒钩 / 深水
- 谁可能是预言家 / 女巫
- 第二天站边和发言策略

最终行动仍必须通过 RuleEngine 验证。

### 输出建议

```json
{
  "type": "wolf_discussion",
  "message": "我倾向先刀4号，他像预言家。",
  "preferred_target": 4,
  "confidence": 0.71
}
```

最终刀人必须单独提交合法 action，不允许讨论文本直接修改 GameState。

---

## R-006 AI Memory 改为增量结构化记忆

### 目标

AI 记忆不得只依赖“最近 N 条 event + 每轮重新正则扫描”。应在事件发生时持续更新结构化 PlayerMemory。

### 建议模型

```json
{
  "facts": [
    {
      "id": "f1",
      "day": 1,
      "text": "2号自称预言家",
      "source_player_id": 2,
      "visibility": "public"
    }
  ],
  "claims": [
    {
      "player_id": 2,
      "claimed_role": "seer",
      "day": 1
    }
  ],
  "beliefs": [
    {
      "player_id": 2,
      "role_guess": "seer",
      "confidence": 0.63
    }
  ],
  "contradictions": [
    {
      "player_id": 5,
      "summary": "Day1 支持2号，Day2称从未相信2号"
    }
  ],
  "vote_history": [],
  "self_history": []
}
```

### 必须记录

- 身份声称
- 金水 / 查杀声称
- 站边变化
- 投票历史
- 被投票历史
- 玩家明确怀疑对象
- 关键矛盾
- 自己过去的发言与判断
- 当前身份猜测与置信度

### 自己的历史

AI 自己说过的话也必须进入 memory。

目标：避免 AI 第二轮否认或忘记自己第一轮公开说过的立场。

### 记忆更新

推荐：

```text
Game Event
  -> Memory Extractor
  -> Update PlayerMemory
  -> AI Prompt Builder
```

Memory Extractor 可以先使用规则 + 轻量 LLM 混合方案，但任何写入都不得突破 InformationScope。

### 长局处理

AI Prompt 不必携带完整聊天历史。推荐：

- 最近少量原始事件
- 结构化长期记忆摘要
- 当前状态

避免仅保留“最后 50 条 event”导致早期关键逻辑消失。

---

## 统一 AI 输出 Contract

建议所有 Provider 最终转换为统一内部 schema。

### 发言

```json
{
  "type": "speech",
  "content": "我目前更怀疑3号。"
}
```

### 投票

```json
{
  "type": "vote",
  "target_player_id": 3
}
```

### 预言家

```json
{
  "type": "seer_check",
  "target_player_id": 5
}
```

### 统一处理

必须处理：

- 非法 JSON
- Markdown code fence 包裹 JSON
- 未知字段
- 非法 player_id
- 超长输出
- 空响应
- Provider 拒答
- 超时
- 重复响应
- 过期 turn 响应

任何模型输出都只能视为 proposal，由 RuleEngine 决定是否合法。
