# 真实模型池、独立 Agent 与费用保护

本版保留明确标识的本地 Mock 练习模式。仅配置 API Key 不会把默认练习座位改成付费模型；房主需要在大厅「AI 座位设置」选择真实模型或自动分配，并保存设置。

## 配置真实模型池

API Key 只通过服务器环境变量或服务器 `.env` 加载。浏览器、公开配置、快照和模型日志均不需要 Key。交付压缩包排除 `.env`，部署时自行设置服务器密钥。

每个模型的唯一键为 `provider:实际 model ID`，例如 `dashscope:qwen-plus`。同一 Provider 可登记多个实际模型；不同昵称、不同性格、同一模型的不同座位都不会增加独立模型数量。

环境变量例子（Key 值留空，不应把实际密钥写入文档）：

```dotenv
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen-plus
DASHSCOPE_MODELS=qwen-plus,qwen-turbo,qwen-max

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
OPENAI_MODELS=gpt-4.1-mini,gpt-4.1

ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-20250514
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
```

`*_MODEL` 指定该 Provider 的默认模型；`*_MODELS` 用逗号登记附加模型，重复 ID 自动合并。`ANTHROPIC_MODELS`、`GEMINI_MODELS` 同样可用。上例展示配置格式，所列模型必须确实受当前账户、区域和接口支持，才能用于真实对局。仅在列表里写入一个 ID 不等于已确认它可访问。

需要禁用模型或声明能力时，可增加 JSON 注册表。它覆盖相同 key 的设置，并与默认模型及 `*_MODELS` 合并：

```dotenv
AI_MODEL_REGISTRY=[{"provider":"dashscope","model":"qwen-plus","enabled":true,"capabilities":["chat","json","stream"]},{"provider":"dashscope","model":"qwen-turbo","enabled":false,"capabilities":["chat","json","stream"]}]
```

`configured` 从相应服务器 `*_API_KEY` 是否存在判断，注册表里的 `configured:true` 不能替代密钥。登记模型默认支持 `chat/json/stream`；声明能力应与实际模型相符。开局分配的真实 AI 模型需要同时支持三项能力；JSON 决策与公开流式发言的故障路由也按各自所需能力筛选。

初始 `healthy=true` 表示没有观察到失败，并非服务器已经主动验证账户权限。调用失败后，内部健康状态影响后备池和开局分配。公开健康状态只根据公开发言、投票或显式探测更新；狼人讨论、技能和搭档私聊的失败不会改变公开健康标志，从而避免按失败时间推测隐藏身份。

## 大厅严格模式与兼容模式

默认勾选「每个 AI 使用独立真实模型（严格模式）」。自动分配先保留房主锁定的模型，再使用尚未占用的健康模型，并优先分散到不同 Provider 与 model。模型不足、锁定模型不可用、能力不足或锁定重复时，服务器阻止开始并给出解释，不会静默把所有角色放到同一真实模型上。

六人房间中，有一个真人就需要五个真实模型来填满其余五个真实 AI 座位；有两个真人则需要四个。显式选择 Mock 的座位是离线练习座位，不占真实模型唯一名额，因此不配置任何 Key 也能完整进行 Mock 对局。

房主取消严格模式并保存后，允许显式兼容复用。界面提示共享模型及相关座位。每个座位仍拥有不同的 `agent_id`、身份、结构化记忆、私有上下文与会话状态；模型路由不复用另一个 Agent 的完整 prompt/history。兼容模式不会凭空增加模型数量。

模型故障切换是一次调用的后备执行，不会把两个存活 Agent 永久合并成一个会话。原绑定模型仍是下一次调用的首选，后备请求使用该 Agent 完全相同的合法上下文。

## 实际模型标签与故障切换

大厅显示绑定模型和健康状态；游戏中公开发言、投票的模型标签显示实际执行模型及「已切换」「模拟兜底」「输出中断」「预算已用完」等状态。搭档执行状态只出现在所属玩家的私有视角。技能和狼队讨论的实际执行信息不会作为公开的座位状态播出，避免暴露神职或狼人身份。

一次真实调用的路由顺序为：绑定模型 → 可配置重试 → 其他已配置、启用、具备当前能力的健康真实模型 → 全部不可用时进入 Mock。超时、连接失败、429、服务端错误、无效 JSON 或不合法的动作格式可触发后备。认证或其他不适合重试的 4xx 不会在同一模型上重复请求，但仍可尝试其他真实模型。

流式发言在输出首个可见字之前失败，可以切换模型重新开始。已输出部分文字后失败时，当前文字保留并结束本次输出，界面显示非阻断提示；不会接上一段从头生成的后备回答。取消、切换回合或提前关闭流时，会关闭上游流并结算已经预留的请求预算。

```dotenv
LLM_TIMEOUT=60
LLM_CONCURRENCY=4
LLM_RETRIES=1
LLM_RETRY_DELAY=0.2
LLM_MODEL_COOLDOWN=20
```

`LLM_RETRIES=1` 表示每个候选模型最多额外重试一次。失败模型暂时移出内部健康后备池；明确绑定的首选仍可获得恢复尝试。`LLM_MODEL_COOLDOWN` 到期后允许重新探测，并不证明故障已经消失。游戏的权威时钟独立运行；它可以在请求超时前结束阶段并取消过期响应。

## 请求限流与预算

默认配置如下：

| 环境变量 | 默认值 | 范围 |
| --- | ---: | --- |
| `RATE_SESSION_PER_MINUTE` | 20 | 每 IP 每分钟创建 session |
| `RATE_ROOM_PER_HOUR` | 10 | 每用户每小时建房 |
| `RATE_PET_PER_MINUTE` | 12 | 每用户、每房间每分钟搭档聊天 |
| `RATE_ROOM_AI_PER_MINUTE` | 180 | 每房间每分钟真实 HTTP 尝试 |
| `AI_GAME_MAX_REQUESTS` | 300 | 每局真实 HTTP 尝试数，包含重试和后备 |
| `AI_GAME_TOKEN_BUDGET` | 250000 | 每局 token 预算 |
| `AI_DAILY_TOKEN_BUDGET` | 2000000 | 全服务器 UTC 每日 token 软上限 |
| `AI_GAME_COST_BUDGET` | 0 | 可选每局估算金额上限 |
| `AI_DAILY_COST_BUDGET` | 0 | 可选 UTC 每日估算金额软上限 |
| `MAX_ACTIVE_ROOMS` | 20 | 同时进行的房间数量 |

限流和活动房间上限设为 `0` 表示关闭相应限制。请求数或 token 预算设为 `0` 表示禁止真实调用；金额预算设为 `0` 表示关闭金额限制，token 和请求数限制仍然生效。

创建 session、建房、搭档聊天和开局的入口超限返回明确的 429，第三方调用不会继续排队。游戏过程中的真实模型预算或房间调用频率超限，路由停止付费尝试并改用明确标识的 Mock；房主可见当前预算和拒绝原因，核心规则和时钟继续运行。

每次真实 HTTP 尝试之前同步预留一个请求和最大估算 token，用于防止并发超支。输入预留采用 prompt 的 UTF-8 字节长度加 128 个封装 token；JSON 输出按最多 600、流式输出按最多 350 预留。第三方返回 usage 时，用实际报告结算；缺失 usage、失败、取消或进程重启遗留的预留会保守计入用量。流式中断时不会把 Anthropic 初始事件中的微小 output 计数误当最终账单。

预留值是保守的容量控制，并非准确 tokenizer 结果。长上下文、失败请求或供应商未报告 usage 时，应用预算可能早于供应商实际额度耗尽。应用显示的 token、金额和供应商账单也可能不同；供应商实际计费、缓存、套餐和价格以其账单为准。账本故障时停止新的真实调用，Mock 仍可用。

计量状态持久化在同一 SQLite 数据库中；刷新页面、重连或重启不会清空已记账用量。再来一局使用新的 game ID，单局计数重新开始，全局每日计数继续累积。当前部署要求单实例、单 worker，额外实例需要共享且原子化的限流和计量服务。

## 可选金额估算

价格以每百万输入／输出 token 的金额表示，所有价格与金额预算必须使用同一种货币单位。填写部署时核实的当前价格；下面的数值仅展示计算格式，不是报价：

```dotenv
AI_MODEL_PRICES={"dashscope:qwen-plus":{"input":1.0,"output":2.0}}
AI_GAME_COST_BUDGET=1.0
AI_DAILY_COST_BUDGET=10.0
```

可用 `AI_INPUT_PRICE_PER_MILLION`、`AI_OUTPUT_PRICE_PER_MILLION` 配置未单独登记模型的默认价格。两者默认都是 `0`，未提供正确价格时金额限制不能替代请求数和 token 限制。仅有 total usage、没有输入／输出拆分时，按较贵的方向保守结算。

## 安全审计记录

`werewolf.models` 日志包含逐 HTTP 尝试的 `model_call`，以及一次调用最终结果的 `model_route`。即使预算在首次 HTTP 请求前就阻止调用，也会记录最终 `budget_exhausted` 和 `failure_reason`。

记录包括 room/game/agent ID、请求类别、请求模型、实际模型和 Provider、后备链、延迟、可获得的 token、配置价格时的估算成本及安全的失败原因。失败原因采用 HTTP 状态码、异常类型或预算原因码。日志不记录 API Key、Authorization header、session token、完整 prompt、私有记忆或供应商错误正文。内部 `router.records` 保存逐次尝试，`router.outcome_records` 保存最终结果，均为有界队列。

模型池、JSON/SSE 路由、并发上下文隔离、公开健康隐私、跨上下文流关闭、usage 与预算结算等回归检查：

```bash
python -m unittest tests.test_models tests.test_model_routing tests.test_limits -v
```

这些测试使用隔离的测试环境和模拟 HTTP，不使用交付包中的真实 API Key。
