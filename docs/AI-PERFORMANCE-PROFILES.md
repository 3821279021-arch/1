# AI Performance Profiles（V3.3）

V3.3 将“游戏必须遵守的硬规则”和“为了成本/体验施加的软限制”分开。

## 四种档位

| Profile | 主要用途 | Prompt budget | Output cap | 历史 | 发言策略 |
|---|---|---:|---:|---|---|
| Economy | 低成本试玩 | 3,500 | 512 | 最近 8 条公开事件 | 280 字，要求简洁 |
| Balanced | 默认娱乐 | 6,000 | 1,200 | 最近 12 条 + 结构化记忆 | 500 字 |
| Unrestricted | Capability Benchmark / 最高性能 | 64,000 | 16,384 | 完整合法历史 | 最高 4,000 字，可主动沉默 |
| Custom | 研究与高级用户 | 1,800–200,000 | 32–65,536 | Compact / Hybrid / Full | 1–4,000 字或传输上限 |

`Unrestricted` 不自动强制某个 provider 的 reasoning 参数，因为不同模型对 `reasoning_effort` / `thinking_budget` 的兼容性不同。需要强制最高推理档时，请使用 `Custom`，例如 `reasoning_effort=xhigh` 并按模型能力设置 `thinking_budget`。

## 永远不会关闭的硬边界

无论选择何种 Profile，以下边界不受影响：

- AI 只能获得该座位合法可见的信息；不会看到其他身份的私有信息或其他用户凭据。
- 所有投票、技能、夜间动作继续由 `RuleEngine` 做合法性校验。
- API Key / Credential scope / SSRF 防护不受性能模式影响。
- 服务端运营者配置的全局 request / token / cost budget 仍然是最后一道保险；Profile 不可绕过服务器预算。自托管要跑满 `Unrestricted` 时，可按成本承受能力显式提高 `AI_GAME_TOKEN_BUDGET`、`AI_DAILY_TOKEN_BUDGET`；如果设置了 `AI_PROMPT_TOKEN_LIMIT`，它会作为 operator 级 prompt 硬上限。
- 系统提示和隐藏推理不会公开到游戏发言中。

## 主动沉默

当 `force_speech=false` 时，模型可以仅输出：

```text
<SKIP_SPEECH>
```

该标记由服务端消费，不会广播给其他玩家；游戏记录只表现为该玩家结束发言。这允许“少说/不说”成为策略，而不是由 orchestrator 强制生成模板句。

## Arena / Benchmark

Arena 配置新增：

```json
{
  "ai_performance_profile": "unrestricted",
  "ai_performance_custom": {}
}
```

或：

```json
{
  "ai_performance_profile": "custom",
  "ai_performance_custom": {
    "prompt_token_limit": 100000,
    "history_mode": "full",
    "max_output_tokens": 32768,
    "reasoning_effort": "xhigh",
    "thinking_budget": 32768,
    "speech_character_limit": null,
    "force_concise": false,
    "force_speech": false
  }
}
```

实验 manifest 会保存 `ai_performance_profile` 与解析后的 `ai_performance`。因此结果可以明确区分：

- **Capability Track**：建议 `unrestricted` 或公开完整 Custom 参数。
- **Efficiency Track**：建议固定 Economy/Custom + 实验总 token/cost cap。

不要把不同 Profile 下的胜率直接当成同一实验条件比较。
