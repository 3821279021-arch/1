# Contributing

Use Python 3.12 and Node 22 for the same checks as CI.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python run.py
```

For Windows use `.venv\Scripts\Activate.ps1`. Mock requires no API key.

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
python -m playwright install chromium
# In another terminal, start the server with Mock/short timings; see docs/development.md.
python tests/browser_v3.py
python tests/browser_v31.py
docker build -t ai-werewolf-dev .
docker run --rm ai-werewolf-dev python tests/container_smoke.py
```

Run `ruff check --fix app tests tools` and `ruff format app tests tools` before a PR.
Do not weaken assertions, remove tests or mark fallback/timeout as a successful model answer.

Add roles through `app/roles.py`: legal targets, night/death hooks and mode validation. Add provider support through `app/providers.py`, credential validation and model registry; retain SSRF checks, usage capture and reasoning filtering. Agents receive `InformationScope.ai_view`, never the full game state.

SQLite changes must be additive and tested against old data. All new snapshot fields need old-save defaults. Research exports require historical ownership and must not change public replay permissions. Never commit credentials, private traces or runtime databases.

Experiments must record seeds, effective parameters, version/fingerprint and sample counts. Any code/config change requires a new experiment. Paid benchmark results need a price table or explicit unknown cost. Submit a manifest and redacted summary through the Benchmark issue template, not raw private artifacts.

PRs need a concise behavioral description, relevant regression evidence and migration/security notes when applicable. See `.github/pull_request_template.md`.
