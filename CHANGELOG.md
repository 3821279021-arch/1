# Changelog

## 3.3.0 — AI Performance Profiles

- 新增 Economy / Balanced / Unrestricted / Custom 四档 AI Performance Profile。
- 将合法信息边界与软性能限制分离：身份隔离、动作校验、凭据保护和服务端总预算始终开启。
- Unrestricted 使用完整合法历史、64k prompt 预算、16k 模型输出上限，并取消强制简洁与强制发言。
- Custom 可配置上下文预算、历史策略、模型输出、reasoning effort、thinking budget、公开发言长度与强制发言策略。
- AI 可用 `<SKIP_SPEECH>` 主动结束公开发言；该控制标记不会出现在公开桌面。
- Arena manifest 记录性能档位与完整有效参数，便于区分 Capability Track 与 Efficiency Track。
- 单次公开发言的系统传输上限提升至 4000 字；具体软上限由性能档位控制。


## 3.2.0 — 产品体验与可玩性

- 最后在线真人离开立即暂停；断线宽限 45 秒，冻结/显式继续、任务取消、闲置卸载与私有数据 TTL 清理。
- 五组本地 BGM、35 段中文主持音频、新技能/死亡音效；独立混音、淡入淡出、音频解锁与失败反馈。
- AI 常规 80～180 字、复杂 250～300 字建议；16 字/秒默认 UI 缓冲，公开思考计时，真实首 token/生成/字数指标独立。
- 身份关联模型收藏、即时搜索、最近真实调用状态、6/9/12 人阵容预设。
- 首页/好友/对战/战绩/我的 App Shell，沉浸游戏态，历史分析和回放入口。
- 六套新增固定板、约束随机、自定义角色池；摄梦人/守墓人/乌鸦/驯熊师使完整角色达 16 个。
- 旧存档/数据库增量兼容；Arena、BYOK 与 V3.1 trace 结构保留，规则和 prompt 新版本只用于新记录。
- Tournament & Evaluation 需求顺延 V3.3，无联赛/Judge/评分等实现。

## 3.1.0 — Reproducible Arena

- Add secure/seeded GameRNG, stable SHA-256 game seeds and persisted RNG continuation; old games remain unseeded.
- Add immutable experiment manifests, JSONL traces, redaction and original-host-only completed-game research export.
- Add serial CLI batches with paired lineups, seat/faction rotation, failure states, crash resume and pre-call token/cost reservations.
- Add summaries with faction/role/seat/agent breakdowns, Wilson intervals, explicit N, latency, usage, fallback and unknown costs; add local-file Benchmark dashboard.
- Add Ruff, gradual boundary typing, coverage artifacts, dependency audit, offline deterministic smoke, Dependabot and gated release workflow.
- Keep the V3 multiplayer, BYOK, custom-role, audio and PWA flows. Normalize Python formatting across the original source/tests.
- Project license awaits the owner's explicit choice; automatic public release is blocked until resolved.

Historical changes remain in `docs/CHANGELOG-V3.md`, `docs/CHANGELOG-V2.3.md` and `docs/CHANGELOG-V2.2.md`.
