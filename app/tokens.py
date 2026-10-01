"""One provider-aware estimator; empirical estimates are labelled, never bytes.

OpenAI uses its local tokenizer when installed. Other providers use explicit
coefficients until measured usage supplies a per-model calibration in LLMRouter.
The tokenizer may fetch its public vocabulary on first use; failures use the
labelled heuristic. No provider inference API is called for estimation.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
COEFFICIENTS = {"openai": 1.05, "anthropic": 1.15, "gemini": 0.95, "dashscope": 0.95}


@lru_cache(maxsize=16)
def _encoding(model: str):
    try:
        import tiktoken
        return tiktoken.encoding_for_model(model)
    except Exception:
        return None


def estimate_tokens(provider: str, model: str, text: str) -> int:
    if not text:
        return 0
    if provider == "openai":
        encoding = _encoding(model)
        if encoding is not None:
            return len(encoding.encode(text, disallowed_special=()))
    chinese = len(CJK.findall(text))
    rest = CJK.sub("", text)
    # JSON punctuation, numbers and short words have proportionally more tokens.
    words = re.findall(r"[a-zA-Z0-9_]+|[^\w\s]", rest)
    latin = sum(max(1, math.ceil(len(word) / 4)) for word in words)
    other = sum(1 for c in rest if not c.isascii() and not c.isspace())
    return math.ceil(chinese * COEFFICIENTS.get(provider, 1.0) + latin + other)
