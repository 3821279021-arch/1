# V3.0.0 验收记录

验收日期：2026-10-01。环境为 Python 3.12.14、Chromium/Playwright、SQLite、Docker。验证对象是本交付包代码与 V3 需求，基线为用户提供的 V2.3.1。

## 自动验证

后端全量回归 **189 项全部通过，52.926 秒**。其中 V3 新测试 67 项：AI 自主性 15、公开发言分析 5、HTTP 集成 8、凭据与协议 19、规则 20；其余是原有规则、隐私、预算、限流、恢复、幂等和实时机制的回归。旧测试只迁移了与 V3 明确冲突的断言：随机座位比对原分配结果、完成即推进、中性弃权兜底、允许合法公开欺骗。

最后调整了赛后技能计数与未知价格显示，相关 HTTP 集成 8 项再次全部通过（10.711 秒）。Python 编译、所有 JavaScript 模块语法和依赖一致性检查均通过。

| 需求 | 验证证据 | 结果 |
| --- | --- | --- |
| AI 自主推理 | 不同模拟模型返回不同票；旧 server 变量不能覆盖；诈身份/谎报/隐瞒保持模型输出；无指定 belief/action/expression plan | 通过 |
| 多人公平视角 | 其他人的查验、狼队消息、私有记忆、凭据不进入非法视角；长期和最小预算下保留自己的真实查验 | 通过 |
| 完成即推进 | 未到期 deadline 下全部狼人、技能、真人结束发言和所有投票提交后立即推进；未完成者仍待提交 | 通过 |
| 随机座位与换座 | 9 真人全员随机占空座、默认无 seat、可选换座、冲突与权限验证 | 通过 |
| BYOK | owner 隔离、加密 SQLite、重启恢复、临时丢弃、session/room scope、掩码、替换/删除、422不回显Key、撤销旧任务 | 通过 |
| 动态模型与多供应商 | OpenAI/Anthropic/Gemini/DashScope/OpenAI-compatible 的动态目录、JSON/SSE、模型参数、手动 ID 兜底、密钥隔离 | 模拟 HTTP 协议通过 |
| 密钥和网络边界 | SSRF、DNS 重绑定、重定向、非法 URL、上游/跨 SSE chunk 回显Key、模型目录回显Key、错误脱敏、旧 revision拒绝 | 通过 |
| 真模型故障处理 | 非法结果、连接失败、凭据撤销与预算耗尽均中性弃权/不用技能，标记原因；不借其他 Key | 通过 |
| 多人数完整局 | 6/9/12 与 custom 8 人由真实调度、Mock 模型和持久化完整跑完 | 通过 |
| 模块化角色 | 12种角色、枪击连锁延迟胜负、守卫、骑士、白痴、狼美人、白狼王、隐狼觉醒和信息隔离 | 通过 |
| 赛后与回放 | 未结束拒绝身份详情；旧局重启后保持；重赛隔离；未参与旧局的新成员不能读；仅公开事件回放；平局不误算赢 | 通过 |
| 容器 | 实际镜像构建与容器内 ASGI/SQLite/恢复码 smoke | 通过 |
| 浏览器布局、模型中心、音频和回放 | 77 项 Chromium 检查，page_errors=[]；本地 WAV play()成功；音频拒播与缺失TTS原因可见；6/9/12/16与横屏无头像重叠；原文回放和重赛旧局 | 通过 |

规则随机压测另外运行了 **40 局、4487 次合法动作**，全部结束，包含 6/9/12 与所有狼系混合 custom；技能覆盖数量见 `playtests/v3-random-games.log`。

浏览器最后一轮 **77 项检查全部通过**。保存/替换/删除凭据使用真实应用端点，只有上游模型目录使用固定模拟响应。覆盖桌面、390×844、375×667和844×390，整页不滚动、阶段与底部操作可见，短屏头像列单独滚动。

## 可复现命令

从项目根目录运行：

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q app tests
```

浏览器先启动服务（验收使用独立临时数据库、关闭测试限流，并加速回合），再运行脚本：

```sh
DATABASE_PATH=/tmp/werewolf-v3-browser.sqlite GAME_TIME_SCALE=0.12 AI_TURN_PAUSE=0.01 AI_CHUNK_DELAY=0.001 RATE_SESSION_PER_MINUTE=0 RATE_ROOM_PER_HOUR=0 RATE_ACTION_PER_MINUTE=0 RATE_JOIN_PER_MINUTE=0 RATE_MODEL_TEST_PER_MINUTE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8012
TEST_BASE_URL=http://127.0.0.1:8012 python tests/browser_v3.py
```

脚本使用系统 Chromium；若不存在则使用 Playwright 安装的 Chromium。Windows 可用环境变量对应命令。生产启动使用 README 的正常命令，不需要测试加速和关闭限流。

Docker Hub 在本环境返回 429，因此使用同一官方 Python 镜像的公开 ECR 镜像源完成构建。生产 Dockerfile 默认仍为 python:3.12-slim，提供 PYTHON_IMAGE 参数供镜像源选择：

```sh
docker build --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim -t ai-werewolf-v3 .
docker run --rm ai-werewolf-v3 python tests/container_smoke.py
```

本管理环境构建时按 cloud-environment-runtime 的证书指导额外使用 `--secret id=system_ca,src=/etc/ssl/certs/ca-certificates.crt`，保持 TLS 验证且不把环境证书写进镜像。容器验证返回 version=3.0.0、asgi_health=true、sqlite_room_creation=true、session_recovery_and_revocation=true。

## 完整局数据

`playtests/v3-api-results.json` 是最后一次实际运行的结果，含人数、胜方、天数、耗时、事件数和赛后人数。所有游戏使用真实应用调度和 SQLite，由 Mock 推理完成；没有用预写胜负替代运行。

## 真实服务与设备的验证边界

本次没有用户有效的真实 API Key。云模型发现、原生 HTTP/流式协议、参数与错误路径使用模拟 HTTP 响应验证；不能据此宣称真实账号权限、云模型能力、模型横向优劣或账单费用已经实测。填入自己的 Key 后可在模型中心连接测试并参与对局。

Chromium 的本地 WAV 播放使用真实 media play()，TTS 开关和失败路径可验证；无声音引擎会给可见原因。浏览器测试不能证明物理扬声器听感、手机静音状态或 iOS/Safari 真机行为。本项目提供同步点击音频解锁和错误反馈，真机试听需要对应设备。

自动测试和截图见 `playtests/v3-*` 与 `previews/v3/`。交付包不包含运行数据库、真实用户凭据、密钥文件、.env 或当前会话令牌。
