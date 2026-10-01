# V2.2 交付与验证记录

2026-09-30。按照 `ai-werewolf-v2.1-requirements-updated.zip` 实现 R001–R014；两份新需求包已比较，更新包增加实时聊天、声音、模型池和 Agent 隔离。保留 FastAPI / SQLite / PWA 与原六人规则。

## 自动化结果

- **91 项 Python 测试全部通过**。既有规则、鉴权、实时、模型动作测试继续通过；新增模型池与严格分配、私有健康状态隔离、流关闭结算、增量记忆、限流预算、生命周期、版本与幂等、真实模型绑定迁移、HTTP 429 和秘密数据检查。
- **100 chunks 专项**：发言期间 SQLite 整局 save=0、全量 snapshot=0；完成后 save=1，重新读取完整发言与下一座位顺序正确。
- 死亡狼人：owner snapshot、增量连接及重连恢复均无死亡后的狼聊；死亡时队友信息冻结。非狼始终收不到狼聊与其他人的搭档消息。
- 同 action_id 重放与重启后重放只确认一次；旧回合 AI 返回丢弃且记录 stale_ai_response；退出有连接的房间后连接被清除，时钟与补位继续正常。WebSocket在断开/ASGI取消时清理收发子任务，并保护收尾，修复偶发的关闭异常；连续20次多人WebSocket关闭/重启压力回归全部通过。
- 狼队两轮顺序 `2→3→2→3`，各次上下文中已有狼聊数为 `0/1/2/3`；讨论不写最终刀人结果，且不能读取其他角色私有查验或内部记忆。
- 高频搭档请求在阈值后不启动额外模型；零请求预算不创建 HTTP client，返回可见 Mock 回退、房主额度状态和合法动作，时钟继续。
- 长局602事件后早期身份声称、自己的立场仍在记忆中；公开查验声称不能替代自己的真实私人查验。
- Python 编译和 `app.js` / `sw.js` Node 语法检查通过。

## 浏览器结果

`tests/browser_v21.py` 使用隔离 API / WebSocket 与 SpeechSynthesis stub 验证：

- 不发送快照时，chunk 在300ms内显示，正文顺序正确；公共/搭档事件及随后快照去重。
- 搭档自己的乐观气泡、回复中、增量回信与去重。
- 旧revision、重赛前旧game_id快照拒绝；离线/在线状态、恢复连接后同 UUID 重发。
- 快速双击只有一次提交；明确401不重放状态动作，会话过期可重新建立入口会话。
- 中文语音、按句入队、座位声音差异、切回合/静音/重连清理，私聊和系统内容默认不朗读。
- Wake Lock请求与释放；实际模型切换状态完整显示；360/390/768/1280宽度无横向溢出。

`tests/browser_smoke.py` 对真实运行的 FastAPI 服务验证两个独立手机浏览器身份同房、搭档聊天隔离、托管、刷新原deadline及完整Mock对局。此次结果：14条公开记录、36个流式chunk、狼人获胜、零JavaScript错误。实际手机布局截图在 `docs/previews/`。

**自动化没有证明真实手机扬声器可听或现场弱网体验。** Web Speech API声音取决于手机系统、浏览器和已安装voice；需在目标iOS/Android设备点击「开启声音」后试听。自动化已验证朗读内容、队列与隐私边界，实际音频仍为设备补验项。

## 真实模型

账号探测 `qwen-plus/qwen-turbo/qwen-max/qwen-flash/qwen3-max` 全部HTTP200且返回合法JSON。两局并行严格模式实测完成，每局五个AI分别绑定五个真实model_key，各Agent独立；共70次真实游戏模型请求，70次成功，0次网络失败，384450个报告token。

首选 `qwen-plus` 人为注入两次超时后，实际 `qwen-turbo` 完成合法投票。后备链、实际模型和切换状态均记录；额外消耗37个报告token。

默认250000单局token预算采用保守预留。较长一局实际报告233464 token时，部分长上下文请求已无法预留容量，触发预算保护；两局共18次可见Mock回退，未继续调用付费模型，均正常结算。原始记录在 `docs/playtests/v22-live-games.json`，探测在 `v22-model-probe.json`。HTTP请求记录不包含Key或完整输入prompt。

最终流处理收尾后再跑一局严格模式：37次真实请求、37次成功、零回退，113个chunk，第2天狼人获胜，约174秒。此回归显式设500000单局token预算，正式默认仍250000；三局真实游戏合计107次请求、107次成功，592156个报告token。记录见 `v22-live-regression.json` 和 `LIVE-PLAYTEST.md`。

## 容器与持久化

最新运行源码使用本环境已缓存的Python3.12依赖基础镜像构建验证镜像，**容器内同样91项测试通过**。此验证没有重新下载Docker Hub基础镜像；标准Dockerfile仍提供Python3.12-slim普通部署入口。

容器健康接口HTTP200、版本2.2.0；不挂载Key时所有真实provider显示未配置。持久数据卷中建房并开局，重启容器后原session、game_id和未过期deadline恢复，revision未回退。

## 交付与运行边界

- 压缩包排除 `.env`、数据库、运行日志与缓存；服务器Key未进入代码、前端或交付文件。
- SQLite只运行单实例、单worker，启动锁阻止重复权威时钟。未接入分布式锁/Redis/PostgreSQL。
- 默认Mock可完整离线练习；真实AI严格模式需要足够实际模型，显式兼容模式才可复用。真实模型首次健康标志表示已配置且尚未观察到失败，不代表账号权限探测成功。
- OpenAI / Claude / Gemini仅协议模拟验证，本次真实调用使用DashScope；云TTS、原生App、成长等级均不在本次实现范围。
- Render配置仍是Starter及持久盘方案；此次没有创建外部部署或收费服务。
- 新增需求、逐项验收映射和配置分别见 `docs/v21-requirements/`、`UPGRADE-TRACEABILITY.md`、`MODELS-AND-LIMITS.md`。

复现：`python -m unittest discover -s tests -v`，浏览器与可选真实实测的命令见README。真实实测会调用模型并可能产生费用，不纳入普通测试发现。
