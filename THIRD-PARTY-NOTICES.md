# Third-party notices

| Component | Source | License / status |
| --- | --- | --- |
| Qwen2 vocabulary | Qwen/Qwen2 tokenizer; `app/tokenizers/README.md` | Apache-2.0, full notice retained at `app/tokenizers/LICENSE-QWEN.txt` |
| FastAPI, Uvicorn, HTTPX, Starlette | Official PyPI packages, versions in requirements.lock | MIT / BSD-3-Clause as distributed by each package |
| Pydantic, python-dotenv, tiktoken | Official PyPI packages | MIT / BSD-3-Clause as distributed by each package |
| cryptography | pyca/cryptography | Apache-2.0 OR BSD-3-Clause |
| Playwright, Ruff, Mypy, coverage, pip-audit, jsonschema | Development packages only | Retain their distributed license notices; not vendored into source |
| WAV prompts, SVG avatars, PNG/PWA icons | Preserved from the supplied V3.0 delivery | No new external assets added. Original provenance must be confirmed by the owner before public redistribution |
| V3.2 BGM and skill/death WAV | Original deterministic synthesis in scripts/generate_audio_v32.py | Generated for this project; project license remains pending |
| V3.2 Mandarin MP3 announcements | Synthesized locally with eSpeak NG 1.51 cmn | eSpeak NG engine is GPL-3.0-or-later, build-time only; no engine or voice data bundled; generated audio provenance is documented in manifest-v32.json |

V3.1/V3.2 implementations are original changes to the supplied project. No source from the reviewed repositories was copied. Architecture references and pinned commits are listed in `docs/V3.1-COMPARISON.md`. A review package's license claims are not permission to copy code from a repository without a verified LICENSE.
