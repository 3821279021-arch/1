"""One provider-aware estimator; empirical estimates are labelled, never bytes.

OpenAI uses tiktoken; Qwen uses its bundled public vocabulary with the same
byte-level BPE and splitting pattern as Qwen2/3. Other models use a heuristic.
Qwen estimation needs no download or provider request at runtime.
"""
from __future__ import annotations

import math
import json
import re
from functools import lru_cache
from pathlib import Path

CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
COEFFICIENTS = {"openai": 1.05, "anthropic": 1.15, "gemini": 0.95, "dashscope": 0.95}
QWEN_PATTERN = r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"


@lru_cache(maxsize=1)
def _qwen_encoding():
    try:
        import tiktoken
        vocab = json.loads((Path(__file__).with_name("tokenizers") / "qwen2-vocab.json").read_text())
        # Invert the reversible bytes_to_unicode alphabet used by ByteLevel BPE.
        values = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
        codepoints = list(values)
        extra = 0
        for value in range(256):
            if value not in values:
                values.append(value)
                codepoints.append(256 + extra)
                extra += 1
        alphabet = {chr(codepoint): value for value, codepoint in zip(values, codepoints)}
        ranks = {bytes(alphabet[char] for char in token): rank for token, rank in vocab.items()}
        return tiktoken.Encoding(name="qwen2-local", pat_str=QWEN_PATTERN,
                                mergeable_ranks=ranks, special_tokens={})
    except (ImportError, OSError, ValueError, KeyError):
        return None


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
    if provider == "dashscope" and model.lower().startswith("qwen"):
        encoding = _qwen_encoding()
        if encoding is not None:
            return len(encoding.encode(text, disallowed_special=()))
    chinese = len(CJK.findall(text))
    rest = CJK.sub("", text)
    # JSON punctuation, numbers and short words have proportionally more tokens.
    words = re.findall(r"[a-zA-Z0-9_]+|[^\w\s]", rest)
    latin = sum(max(1, math.ceil(len(word) / 4)) for word in words)
    other = sum(1 for c in rest if not c.isascii() and not c.isspace())
    return math.ceil(chinese * COEFFICIENTS.get(provider, 1.0) + latin + other)
