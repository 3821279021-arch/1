# V2.3.1 验收记录

验证环境：2026-10-01，Python 3.12，锁定依赖，Chromium 151。结果针对用户提供的 V2.2 工程升级。

已通过 **122 项自动测试**，包括原 91 项、V2.3 新增 22 项和本次真实实测修复新增 9 项。三套浏览器、语法、依赖与 Docker 验证也通过。

已使用用户授权的 DashScope Key 完成五局分批真实对局，176 次对局/搭档请求全部成功；最终公开表达保护以最后一局及最终专项为准。真实 Usage 的预算误差中位数约 15%（含安全余量），手机尺寸浏览器真实私聊两次通过。完整问题、修复、分批证据和限制见 [真实实测报告](LIVE-PLAYTEST-V2.3.1.md)。

## 已验证

- 原 91 项规则、限流、信息隔离、实时、过期 AI、幂等、模型路由回归保留。旧字节估算断言改为验证统一 Token 估算。
- 新增记忆上限、600 条语义事件压缩、每日摘要/私有来源、自然怀疑低置信度、声明冲突、票压衰减、自己的查验、狼队计划隔离。
- 规则引擎连续运行 22 个完整游戏日、超过 300 次合法动作，六名玩家全部存活（技能/投票弃权），检查每名玩家的记忆上限和 Prompt 大小。
- 真实 HTTP 传输使用 MockTransport 验证四家 Provider schema/结果/Usage、HTTP Client 单实例复用及关闭、修复重试、策略回退、Gemini 推理用量、成本重启恢复。
- 验证 SQLite 慢写入时事件循环继续运行；验证归档后实际删除快照、事件和私有记忆，仅留下匿名汇总。
- API 验证锁房/密码、房主踢人、重入限制与解除、跨设备恢复/旧码失效、旧 Token 撤销、WebSocket 断开与权威快照、CSP/其他安全头、房间限流、未结束游戏的成本明细隔离。
- 三套浏览器测试：原实时/流式/去重/音频/乐观宠物/离线重连/幂等；真人与 AI 完整 Mock 对局；新增锁房/密码/踢人/重入/恢复/旧设备断开。页面 JavaScript 无错误，原 360/390/768/1280 宽度无横向溢出。
- Python compileall、所有 ES Module/Service Worker 语法检查、Docker 镜像构建和容器真实 ASGI/SQLite/会话恢复 smoke。

最新结果：`playtests/v231-verification.json`；原 V2.3 合成内存比较保留在 `playtests/v23-memory-benchmark.json`；真实 Qwen 私聊预览：`previews/v231-live-mobile-pet.png`。

## 30 天合成成本比较

对两个版本输入相同的 30 天、每天 18 条带角色声明/怀疑/站边的事件；两版 Prompt 都用同一 V2.3 DashScope 经验估算器计算，排除“把字节改成 Token”的测量口径差异。结果由 JSON 文件记录：总提示量约减少 94%，第 10/20/30 天的新提示大小基本稳定。

这是一种反复声明的压力场景，不代表常规短局会节省同样比例，也未证明策略实战质量。数据均为估算值，并非 Provider 的真实收费 Usage。

## 尚未实测

- OpenAI/Claude/Gemini 未配置真实 Key，仅通过协议模拟。
- 真实长局输入下降 30% 未做同条件对照；完整真实对局最长 3 天，30 天历史通过合成输入调用真实 API 验证。
- 手机真实锁屏 30 秒、Wi-Fi/蜂窝切换及 Safari 实机音频未做设备验证；浏览器已覆盖离线/在线、重连快照及仪器化 WebSpeech，实际音量仍需手机验收。
- 未验证多实例部署，本版继续明确限制一个进程/一个 worker。

## 复现

```bash
pip install -r requirements-dev.txt
python -m compileall -q app tests
python -m unittest discover -s tests -p 'test_*.py' -v
for f in app/static/app.js app/static/js/*.js; do node --input-type=module --check < "$f"; done
node --check app/static/sw.js
python -m playwright install chromium
# 第二个终端启动以下服务器，保持运行
GAME_TIME_SCALE=0.12 AI_TURN_PAUSE=0.01 AI_CHUNK_DELAY=0.001 python run.py
# 回到测试终端
python tests/browser_v21.py
python tests/browser_smoke.py
python tests/browser_v23.py
# 可选：使用原 V2.2 源码压缩包复现合成对比
python tests/benchmark_memory.py --baseline-zip /path/to/ai-werewolf-v2.2.zip
docker build -t ai-werewolf:v2.3.1 .
docker run --rm ai-werewolf:v2.3.1 python tests/container_smoke.py
```

CI 不需要 Key。真实模型测试需在服务器配置可用模型和价格，再按 `tests/live_dashscope.py`、`tests/live_v23_checks.py` 和 `docs/LIVE-PLAYTEST-V2.3.1.md` 运行；该步骤会产生真实模型费用。V2.2 历史实测文件保留作参考，不作为 V2.3.1 实测证据。交付包不包含本次测试密钥。
