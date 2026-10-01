# V2.3 推荐路线图

## Milestone A：成本与稳定性

改动：

- 修复 token estimator
- Memory 上限与摘要
- prompt 去重
- provider usage 指标
- fallback 原因统计

完成标准：

- 长局不再因为明显高估 token 频繁进入 Mock
- 后期 prompt 大小趋于稳定
- 可输出一份每局模型成本报告

---

## Milestone B：模型基础设施

改动：

- 复用 `AsyncClient`
- Provider Adapter
- schema output
- 统一 retry / timeout / error taxonomy

完成标准：

- 四个 provider 共用同一上层调用接口
- provider 切换不修改业务逻辑
- 动作 JSON 无效率明显下降

---

## Milestone C：AI 策略层

改动：

- `GameBeliefState`
- claim / vote / contradiction 规则化
- 策略与表达分离
- 狼队隐藏策略

完成标准：

- Agent 的核心判断可以在不调用 LLM 时被打印/测试
- 切换表达模型后行动策略基本保持一致
- 测试可以断言某类事实会改变 suspicion / credibility

---

## Milestone D：多人可靠性

改动：

- 踢人
- 锁房
- 身份恢复
- token revoke
- room/session 限流
- 重连补状态

完成标准：

- 房主可以处理陌生占座
- 浏览器缓存丢失后有恢复方案
- 网络切换不导致重复动作

---

## Milestone E：工程化

改动：

- JS 模块拆分
- 数据清理任务
- 依赖锁定
- CI
- 结构化日志

完成标准：

- PR 自动执行测试
- 生产依赖可重复安装
- Archived 数据不会无限增长

---

## V2.4 再考虑

建议把以下内容放到后续版本，而不是挤进 V2.3：

- 9 / 12 人
- 更多角色
- 观战
- 完整回放 UI
- 排行榜
- AI 等级成长
- 好友系统

原因：这些功能会扩大状态空间，应该建立在 V2.3 的模型、记忆、存储和多人基础设施之上。
