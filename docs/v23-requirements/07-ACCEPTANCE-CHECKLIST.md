# V2.3 验收清单

## Token / Memory

- [ ] 中文 prompt 的 token 估算误差中位数 < 20%
- [ ] 每种长期 memory 数据都有上限或摘要策略
- [ ] 进入新一天后历史能够压缩
- [ ] 长局 prompt 大小不会持续线性增长
- [ ] 记录 estimated / actual token
- [ ] 记录 fallback reason

## LLM

- [ ] HTTP Client 连接复用
- [ ] OpenAI / Claude / Gemini / DashScope 使用统一结果结构
- [ ] 动作结果经过 schema 验证
- [ ] 非法模型输出不会绕过 RuleEngine
- [ ] Provider fallback 不会丢失 Agent 核心策略状态

## AI

- [ ] 存在独立的结构化 belief/strategy 状态
- [ ] claim 冲突可以由服务器确定性识别
- [ ] 投票行为进入可信度/怀疑度计算
- [ ] 旧信息存在衰减或摘要
- [ ] LLM 表达阶段禁止添加新的“事实”

## 多人

- [ ] 房主可以踢出玩家
- [ ] 房间可以锁定
- [ ] 有跨设备/清缓存后的身份恢复方案
- [ ] token 可以 revoke
- [ ] join/chat/action 有房间或 session 维度限流
- [ ] WebSocket 重连不重复动作

## 数据

- [ ] Archived 房间存在真正的数据清理策略
- [ ] session 有过期清理
- [ ] 私有 AI 记忆有明确保留周期
- [ ] 数据库写入不会长时间阻塞 async event loop

## 前端 / 工程

- [ ] `app.js` 拆分为职责清晰的模块
- [ ] 关键生产依赖版本锁定
- [ ] CI 自动运行测试
- [ ] CI 有语法/构建 smoke test
- [ ] 添加 CSP / nosniff / Referrer-Policy 等基础响应头

## 回归测试

- [ ] 原有规则测试全部通过
- [ ] 信息隔离测试全部通过
- [ ] AI 过期动作丢弃仍有效
- [ ] action_id 幂等仍有效
- [ ] 真实 provider 至少进行一局完整回归
- [ ] 至少测试一次 20+ 回合长局
