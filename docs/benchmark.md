# V3.1 Benchmark

Python 3.12，先安装 requirements.txt。Runner 串行驱动现有 RuleEngine、AIOrchestrator 和 InformationScope；不另造规则，不模拟浏览器。

```bash
python -m tools.arena run --config experiments/mock.json --games 4 --seed 20261001
python -m tools.arena run --config experiments/qwen-vs-openai.json --games 100 --seed 20261001 --dry-run
```

实际付费运行去掉 `--dry-run`，并提供 `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` 等运行时变量。配置只接受 `credential_env` 引用，拒绝明文 key、headers 和未知字段。缺失凭据是 failed，不会借用其他模型。不要为了演示付费调用而提交密钥。

每个 agent 记录 id、provider、model、personality、parameters。可配置 temperature、top_p、reasoning_effort、thinking_budget、enable_thinking 和 max_output_tokens。模型采样 seed 只在兼容 Chat Completions、DashScope/Gemini 端点支持；OpenAI Responses/Anthropic 在配置阶段拒绝不支持的 seed。供应商仍可拒绝某模型不支持的参数，trace 保留实际参数与错误类别。

`lineups` 定义一个或多个模型组合，短列表按顺序循环填满座位。`seat_policy=fixed` 固定座位；`rotate` 每轮轮换所有座位，覆盖角色与阵营；`random_seeded` 使用独立派生 RNG 洗牌。角色由规则 roster + 游戏 seed 确定，不在 runner 中修改角色。

`paired_seeds=true` 时一个 seed 被所有 lineup 与整组换座共用，然后才派生下一个 seed。换座不会改变同组 seed 的角色布局。例：6 座位、2 lineup 时，前 12 局是一组配对，下一组使用新 seed。未跑完整组也允许，但需在报告中说明不平衡，不能声称完整阵营平衡。关闭 paired_seeds 则逐局派生。

默认实验 id 为规范化配置 SHA-256 的前 12 位，也可指定 `--experiment-id my-test`：

```bash
python -m tools.arena run --config experiments/mock.json --experiment-id my-test
python -m tools.arena run --config experiments/mock.json --experiment-id my-test --resume
python -m tools.arena run --config experiments/mock.json --experiment-id my-test --resume --retry-failed
```

已完成局不会重跑；running 表示中断局，重启后从该局初始状态重跑，保留前次 `.attempt-N.jsonl`。failed/timeout/invalid_action/budget_exhausted 只有显式 retry_failed 才重试。恢复不允许改 games、seed、规则、模型参数或源代码指纹；变更需新建实验。单局中间动作的逐步恢复是后续能力，当前以局为恢复单元。

目录：

```text
manifest.json          # 不可变版本/配置/seed/参数/环境
arena.sqlite3          # 复用 room_events、telemetry、completed_games；新增实验索引
status.json            # 持久结果的便携快照
games/game-0001.jsonl  # 逐事件 flush，私有研究 trace
summary.json           # 指标、观察记录、对局状态
summary.csv            # 分维度 wins / N / rate / CI
```

目录权限为 0700，文本 artifact 0600，Git/Docker 默认忽略。只使用 public replay 来提供公开观看；研究 trace 的 audience 是信息分类，不会自动给所有玩家权限。

限制：最多 10000 局、显式单 Provider 调用并发 1、逐局超时、最大动作数、重试次数、token 预算与可选成本预算。配置 `prices` 为 `provider:model: {input, output}`，单位为每百万 token 的同一货币价格，配套 `price_table_version`。启用成本上限时所有付费模型必须有价格。预算在请求前预留并持久化；已知 usage 返还差额，未知 usage 保守保留，不在重启时重置。预算是估算上限，供应商实际计费以账单为准。fail_fast 遇失败停止，预算耗尽总是停止；其余计划局不伪装成完成。

退出码：0 全部完成；1 配置错误；2 有失败或未完成但已生成汇总；3 基础设施故障/中断。

指标的单位：阵营/角色/座位/模型是玩家参与次数，N 不是独立游戏数。同局和配对 seed 结果相关，Wilson CI 是描述性区间。真实模型比较应报告完整配对组，后续增加按组 bootstrap。平局不是任何阵营胜利。failed 局从胜率分母排除，但其模型调用资源/错误仍计入汇总；应同时报告完成比例，避免选择偏差。

投票命中表示投给对立阵营；夜间技能命中是明确的简单描述口径（summary 含定义），不宣称代表最佳策略。unknown cost 用 null；usage 缺失显示有效样本 N 和估算输入，不以 0 冒充已知值。Fallback/成功率都有分母；Mock 不代表真实模型能力。

访问 `/benchmark` 选择一个或多个 summary.json，按实验、模型、Provider、角色、阵营、座位、板子、日期、状态过滤。玩家表按筛选统计；资源卡为完整实验统计；对局状态表按实验/板子/日期/状态筛选。首版任务入口为 CLI，不开放 HTTP 创建/运行/取消长任务。
