# 可复现边界与兼容性

普通模式用 SystemRandom，身份、会话与恢复码始终使用 secrets。实验必须同时提供 experiment_id + game_seed；禁止有 seed 无实验上下文。规则座位、角色洗牌和环境随机入口统一通过 GameRNG。Mock 自己的策略 RNG 与环境 RNG 分离；模型真实采样参数另行记录。

逐局 seed 使用 `sha256(f"{experiment_seed}:{index}")` 的前 8 字节转整数，不用 Python hash。配对组按组 index 派生。独立换座 RNG 再派生，不能提前消耗角色 RNG。`rng_algorithm=python-mt19937-v1`；环境记录 Python 版本。

GameRNG 每次随机操作把完整状态写进 WerewolfGame 的可序列化 rng_state。SQLite 保存/恢复后下一次随机选择一致，不仅第一次身份牌可复现。experiment_id、seed、规则/提示版本写入 dump 与 trace。旧快照缺字段时恢复为无 seed，并注明历史 v3.0 版本；数据库只添加 experiments 与 experiment_games 表，原身份/房间/回放/凭据表保留。

保证范围：相同代码、Python/依赖版本、seed、规则与模型分配计划得到同样环境布局和规则随机序列。真实玩家行为不同会改变后续环境状态；云端模型的路由、供应商版本、服务调度与输出可能改变，不能承诺逐字一致。日志时钟、会话 ID 等安全元数据不要求相同。

manifest 保存 app/git/rules/prompt 版本、有效配置 SHA-256、Python/app/runner 源码与 requirements.lock 指纹、提示源码 SHA-256、模型/Provider/参数和价格版本。恢复核对指纹；同名实验不会静默覆盖。无 Git 时 commit 为 null，源码指纹仍可定位内容。

Trace schema 1.0.0 遵循需求包 JSON Schema，未知兼容字段可忽略；破坏字段语义需新 major。JSONL 每行记录 schema_version、experiment_id、game_id、seed、seq、timestamp、type、audience、payload。实际可展示模型输出、prompt 与结构化动作允许保留；供应商隐藏 reasoning/thinking 字段会移除。所有凭据/会话/恢复码字段和已知运行时 key 都脱敏。

研究 API 只导出已经持久化的事件与 telemetry，旧普通游戏没有捕获的 prompt/raw 不会凭空补出；CLI 实验完整保留模型 prompt/最终输出。public replay 只读 public audience，与研究导出是独立权限。

验收包括：同 seed/不同 seed 布局，跨进程 seed，SQLite RNG 续接，旧快照与旧表迁移，离线两次完整批跑对比，trace schema/权限/脱敏，以及 V3 的现有规则、多人、BYOK、浏览器与容器回归。
