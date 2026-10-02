# 开发与验证

从零安装及规范见 [CONTRIBUTING](../CONTRIBUTING.md)。Python 配置统一在 pyproject.toml；保留运行依赖与开发依赖的约束锁。类型检查覆盖 Game、RuleEngine、SQLite、Provider、Registry 及全部 Arena 新模块；未注解的历史内部函数逐步增加覆盖，不声称全仓 strict。

单元与统计 smoke：

```bash
ruff check app tests tools
ruff format --check app tests tools
mypy
coverage run -m unittest discover -s tests -p 'test_*.py'
coverage report
coverage xml
coverage html
pip-audit -r requirements.txt
python -m tools.arena.smoke
```

浏览器（安装 Playwright Chromium，Linux 缺系统库时执行 `python -m playwright install --with-deps chromium`）：

```bash
python -m tools.arena run --config experiments/mock.json --games 2 --output test-artifacts/arena --experiment-id browser-fixture
# 如该目录已有相同配置结果，加 --resume。
GAME_TIME_SCALE=0.12 AI_TURN_PAUSE=0.01 AI_CHUNK_DELAY=0.001 uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另一终端：
python tests/wait_for_server.py
python tests/browser_v3.py
python tests/browser_v31.py
python tests/browser_v32.py
node --test tests/speech_buffer.mjs
```

CI 生成 coverage XML/HTML、依赖审计 JSON、浏览器报告/截图。coverage 首次记录真实水平，后续调整须说明理由；不为了数字镜像实现造测试。审计不预设漏洞豁免。

```bash
docker build -t ai-werewolf-dev .
docker run --rm ai-werewolf-dev python tests/container_smoke.py
python scripts/build_delivery.py --output dist
```

Tag 发布先检查 VERSION/pyproject/app 版本一致，验证许可证已决，再跑检查/单测/确定性批跑/Docker，生成源码包和测试报告。源码包不包含 `.env`、runtime keys、SQLite、私有 experiment、Git 或缓存。私有许可待决的本地交付可以生成，公开 Release 会被 gate 拒绝。
