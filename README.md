# 月下狼人杀 V2.3

在原 FastAPI 手机网页项目上升级的 **真人 + AI 玩家 + 私人 AI 搭档** 实时狼人杀平台。保留原来的 6 人规则、四家模型路由、Docker、Render 和 PWA。无需 API Key 可以完整游玩。

## 启动

推荐 Python 3.12（本版本的依赖锁定与 CI 环境）。

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell：.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

打开 http://127.0.0.1:8000 。同一 Wi-Fi 的手机打开电脑的局域网 IP 加 `:8000`。服务监听 `0.0.0.0`。

1. 选择名字、座位和速度，创建房间。
2. 复制邀请链接给朋友；朋友在开始前选择空座或 AI 座位加入。每个浏览器拥有独立凭证和私人搭档。
3. 房主可配置空座位的模型、性格；点击开始，未入座位置自动由 AI 补齐。
4. 等待自己的回合，提交技能、发言或投票；可以提前结束发言。
5. 其他人发言时打开「🐾 AI 搭档」私聊。代打按钮只授权下一次动作；托管持续接管，收回控制权立即取消尚未提交的 AI 操作。

即使关闭所有浏览器，服务器也继续处理计时和 AI 回合。刷新或断线重连恢复当前阶段与原 deadline。浏览器凭证保存在 localStorage。首次创建会话会显示恢复码，请保存；清缓存或换设备后，在「身份恢复与会话」输入恢复码即可恢复所有原座位。使用后恢复码和 Token 都会更新，旧设备立即断开。邀请链接只含房间号。

## V2.3 改进

- Token：OpenAI 使用 tiktoken；其余 Provider 使用经验系数、按实际 Usage 校准与 15% 余量，统一预算入口，不再把中文 UTF-8 字节数作为 Token 数。
- 记忆：每种历史集合有上限；保留有来源的角色/查验/票型/自身选择摘要，进入新一天生成结构化每日摘要，旧怀疑按天衰减。
- AI：新增 `GameBeliefState`。默认服务器策略决定动作，模型表达性格；模型不可用时继续使用相同策略。可通过 `AI_STRATEGY_MODE=model` 允许模型在合法范围内选择。
- 模型：复用 HTTP 连接，统一 Adapter/Result/Usage；OpenAI、Claude、Gemini 使用原生 schema/tool 约束，DashScope 使用 JSON 模式和相同本地动作验证；默认有一次修复重试。
- 多人：开局前房主可锁房、设置密码、踢人和重新允许入座；恢复码、Token 轮换/撤销、房间与会话限流、重连权威快照。
- 存储：事件、玩家记忆和模型成本记录有独立表；异步路径把 SQLite 写入移到线程，原子保存快照与语义事件；归档保留期后真正清理私有数据。
- 前端：ES Modules、统一客户端 store、独立 WebSocket transport；加入 CSP、nosniff、Referrer-Policy、Permissions-Policy。
- 工程：关键依赖和传递依赖锁定，新增 GitHub Actions 的规则/安全/长局/浏览器/语法/Docker 检查。

房主管理入口在大厅「房主 · 入座管理」；密码加入入口在首页「身份恢复与会话」。结束后房主可下载本局模型成本报告，费用记录支持服务重启后读取。尚未结束的对局（包括提前关闭）仅提供预算总数，详细调用时机不会泄露角色。

升级及验收说明：[docs/CHANGELOG-V2.3.md](docs/CHANGELOG-V2.3.md)、[docs/VALIDATION-V2.3.md](docs/VALIDATION-V2.3.md)。

合成 30 天场景已证明提示大小趋于稳定；真实 Provider 的 Token 误差与完整实战仍需使用自己的可用 Key 验证，本包未声称完成这些实测。

## 保留的 V2.2 功能

- 独立 room_id/game_id、多人混合房、AI 座位预设；真人优先入座，开始时再补 AI。
- 带 state_revision、turn_id、event_id 的实时增量消息；流式期间不整局写库或广播快照，完成后保存，重连去重。
- action_id 幂等、过期动作/AI 丢弃；服务端过滤死亡狼人后续频道，保留其生前历史。
- Rule Engine 统一验证角色、阶段、存活、轮次、目标、药剂与重复动作；所有 AI 操作经过相同验证。
- InformationScope 先过滤数据，再提供给浏览器、AI 玩家和宠物。公开、狼队、私人搭档三个频道在服务端隔离；游戏结束才公开身份。
- 服务器时钟、逐玩家独立发言、AI 原生 SSE 发言转 WebSocket chunks、自然停顿、统一限时投票。
- 玩家投票阶段只公布提交状态，结束后统一公布目标、弃票和平票结果。
- 宠物副驾、商议、单次代打、托管；6 种性格和可调表达参数进入模型上下文，也影响 Mock 发言。
- 记忆区分公开事实、私人已确认信息与猜测，保存发言、票型、怀疑度、待验证身份推测、立场变化和既往选择。
- SQLite 保存房间、原 deadline、聊天、宠物、身份凭证哈希；预留长期偏好/统计数据表和 API。当前长期功能保存建议长度和策略偏好，未实现成长统计或竞技等级。
- 精确到实际 model ID 的模型池；每座位独立 Agent，严格模式分配独立真实模型，显式兼容模式才允许复用。
- 首选模型重试、其他真实模型自动切换、最终 Mock 回退；实际模型/回退状态可见。流式中断保留已输出文字。
- Session、建房、搭档和房间 AI 限流；单局请求/token预算、全局每日预算和并发活跃房间上限。计费尝试与预留额度持久化。
- 夜间狼人按顺序两轮回应队友，最终夜杀单独验证；事件增量记忆保存早期身份声称、票型与自己立场。
- 房间按需加载、闲置卸载、归档、关闭及再来一局；新局重新分身份并清空私有状态。
- 手机圆桌、夜间主题、按天分组历史、私人搭档抽屉、安全区域和 PWA 静态缓存。
- 用户点击开启中文公开 AI 发言语音，各座位不同声音参数；停止/静音、切回合清理，搭档朗读另行开启。
- 本人回合提醒、可选震动、保持亮屏、安装引导及断网/同步状态。

## 房间规则

固定 2 狼人 + 预言家 + 女巫 + 2 村民。好人消灭所有狼人获胜；狼人数量达到好人数量获胜。40 天仍未结束判平局。

标准速度：狼队讨论 30 秒，夜杀/查验/女巫各 15 秒，发言 30 秒，遗言 20 秒，投票 15 秒。快速发言 15 秒，慢速 60 秒。AI 发言完成后默认停顿 1.5 秒，不强制等满上限。

夜间角色阶段固定计时，避免通过动作完成速度泄露角色。狼人最终选择需要**存活狼人的严格多数**，没有多数不击杀。投票最高票平票或全部弃票时无人出局。夜杀、查验、投票可明确选择弃权。技能超时默认不使用；女巫未用解药时可见夜杀目标，解药用完后不再收到该信息。允许女巫自救及同夜使用两药，禁止自毒。夜间死亡与放逐玩家均有遗言；胜负已达成则立即结束。出局者可以观战和私人聊天，不能投票或使用技能。

## 真正的模型

复制 `.env.example` 为 `.env`，只填写服务器环境变量中的 API Key。前端不会收到 Key。模型名可按你的账号设置；默认 OpenAI `gpt-4.1-mini`、Claude `claude-sonnet-4-20250514`、Gemini `gemini-2.5-flash`、DashScope `qwen-plus`。房主在开始前选择 AI 座位模型；宠物在偏好面板选择自己的模型。

OpenAI Responses、Claude Messages、Gemini streamGenerateContent、DashScope Chat Completions 均接入原生 SSE，仅转发公开发言文本，过滤推理块。未配置模型在大厅不可选；显式 Mock 可离线练习。真实模型先尝试其他健康真实模型，全部不可用或预算耗尽才转 Mock；超过服务器 deadline 的响应会被丢弃。DashScope 已使用真实 Key 做在线实测，记录见 `docs/LIVE-PLAYTEST.md`；其余三家仅完成协议模拟测试。

默认并发上限 4，可用 `LLM_CONCURRENCY` 调整。`LLM_TIMEOUT` 控制单次模型超时；`.env.example` 推荐 12 秒。每次真实 HTTP 尝试（包括重试和后备模型）先预留请求、token 及可配置价格的费用额度，再按可获得的 usage 结算。缺少 usage 时保守记账。预算属于本服务，不代表云厂商账户的全部消费；费用估算需配置正确价格。完整配置见 [MODELS-AND-LIMITS.md](docs/MODELS-AND-LIMITS.md)。

DashScope 配置：在服务器 `.env` 中填写 `DASHSCOPE_API_KEY`，`DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`，`DASHSCOPE_MODEL=qwen-plus`。房主在 AI 座位设置选择 Qwen，宠物在模型选项中选择 Qwen。默认座位仍为 Mock。真实模型自动分配需要注册实际模型池，例如 `DASHSCOPE_MODELS=qwen-plus,qwen-turbo,qwen-max,qwen-flash,qwen3-max`。这些 ID 已在本次账号实测成功，其他账号须核对可用性。严格模式五个 AI 需要五个不同真实模型；不足会提示并阻止开局。可明确取消「独立模型」进入兼容复用模式，共用座位会显示警告。

## 数据与部署

本地默认数据库：`data/werewolf.sqlite3`，可通过 `DATABASE_PATH` 修改。备份时包括 SQLite WAL，或在服务停止后复制数据库。不要把数据库、浏览器凭证或 `.env` 打进公开代码包。

**当前必须运行一个 uvicorn worker / 一个服务实例。**启动锁阻止两个时钟同时写同一个 SQLite。若需要横向扩容，需要迁移数据库并增加分布式房间锁和发布订阅；存储与房间管理已有独立边界，但本版本未接入 PostgreSQL/Redis。

Docker：

```bash
docker build -t ai-werewolf-v2 .
docker run --rm -p 8000:8000 -v werewolf-data:/app/data ai-werewolf-v2
```

加入服务器 Key 可使用 `--env-file .env`。持久卷保证重新创建容器后恢复数据。

Render：仓库已带 `render.yaml`，连接 Blueprint 即可。**配置使用 Starter 服务和 1GB 持久盘，会产生 Render 费用**；这样服务重启/重新部署才保留对局。免费服务文件系统临时，若改为免费试玩，数据库不保证跨部署保存。此交付未替你创建或部署任何收费服务。

手机公网访问需要部署后的 HTTPS 地址；HTTPS 支持添加到主屏幕。Service Worker 只缓存静态资源，始终不缓存鉴权 API 或私人状态。离线页面会提示断线，服务器时钟继续运行。

## 测试

```bash
python -m unittest discover -s tests -v
node --check app/static/app.js
```

可选浏览器验收：

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
# 终端一：加速验收服务（不要在正式游玩中设置这些变量）
GAME_TIME_SCALE=0.2 AI_TURN_PAUSE=0.2 python run.py
# 终端二
python tests/browser_smoke.py
# 浏览器实时事件、声音路由、弱网和重复操作专项（API/WS使用隔离模拟）
python tests/browser_v21.py
```

Windows 可使用 PowerShell `$env:GAME_TIME_SCALE="0.2"` 等环境变量设置方式。浏览器脚本检查 360/390/768/1280 宽度、两个浏览器加入、宠物私聊、托管、刷新保留 deadline、完整 Mock 对局和 JavaScript 错误；截图写入 `test-artifacts/`。完整对局自动化测试也用缩短的时间比例，正式默认值仍为真实秒数。

## 项目分层

```text
app/game.py          每房间可序列化状态、玩家、宠物和性格
app/rules.py         权威规则、阶段和命令验证
app/scope.py         唯一信息权限边界
app/rooms.py         房间锁、服务端时钟、AI 调度和私密广播
app/persistence.py   SQLite 和长期记忆接口
app/runtime_lock.py  跨平台单进程锁
app/ai.py            仅接收 PlayerView 的模型/宠物编排
app/memory.py        有界事件记忆、跨天摘要与私有来源
app/strategy.py      结构化信念和可解释服务器策略
app/tokens.py        统一 Token 估算
app/providers.py     Provider Adapter 与动作 schema
app/model_registry.py 精确模型池、可用性、严格分配
app/limits.py        限流、并发预算预留和持久化记账
app/llm.py           真实模型切换、原生流式输出与安全审计
app/main.py          FastAPI HTTP / WebSocket
app/static/          手机网页与 PWA
```

主要 API：`POST /api/session` 创建设备凭证；其余房间接口使用 `Authorization: Bearer <token>`。`GET/POST /api/rooms`；`POST /api/rooms/{id}/join|start|configure|action|wolf-chat|leave|close|rematch`；`PATCH /api/rooms/{id}/pet`；`POST /api/rooms/{id}/pet/chat`；`GET/PUT /api/pet/memory`。WebSocket `/ws/{id}` 在第一帧接收 `{ "token": "..." }`，可带可选 `last_event_id`；成功后发送合法 `state_snapshot`，随后推送带生成时版本号的增量事件。用户动作携带 UUID `action_id`、`expected_state_revision` 和 `turn_id`，重试保留同一 UUID。旧版无 UUID 请求暂兼容，但不获得动作重放确认。鉴权 token 不放入 URL。V2.3 新增恢复/轮换/撤销、房主管理和成本报告，详见 [API 增量](docs/V23-TRACEABILITY.md)。

V1 的浏览器驱动 `step` 接口已由服务器调度器替代，不支持旧版前端直接调用；旧「主持人视角」接口返回 403，避免在对局中泄露身份。

## 主动实测（会调用真实 API）

`PYTHONPATH=. LIVE_GAMES=2 python tests/live_dashscope.py` 使用服务器 `.env` 的 DashScope Key，注册上述五个模型，并行跑两局严格独立模型对局，检查真实后备接手、信息边界和私人搭档聊天。它不会被普通 unittest 自动运行；每次最多 250 个模型请求、八分钟。原始实测输出在 `test-artifacts/live-dashscope.json`，其中没有 API Key。

房间默认五分钟无连接且非进行中则卸载内存；无连接大厅六小时、已结束房间十四天后归档，会话三十天过期。关闭房间保留可授权读取的历史；结束后「再来一局」回到大厅，保留真人席位与 AI 配置，重新生成游戏 ID、身份、Agent 与局内记忆。

本次需求原文和验收映射见 `docs/v21-requirements/`、`docs/VALIDATION.md`。Web Speech API 已做浏览器队列与权限测试，实际手机是否有中文声音取决于设备和浏览器，需要在手机上点击开启后试听。
