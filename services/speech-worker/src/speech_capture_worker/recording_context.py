"""Validation and evidence-safe use of optional per-recording context."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from speech_capture_worker.errors import InvalidJobRequest

RECORDING_CONTEXT_OPTION = "recording_context"
RECORDING_CONTEXT_SCHEMA_VERSION = "1.0.0"
RECORDING_CONTEXT_PROCESSING_VERSION = "2026-08-02.1"
MAX_RECORDING_CONTEXT_CHARACTERS = 50_000

_TERM_CHARACTERS = re.compile(r"[\u3400-\u9fffA-Za-z0-9·._-]")
_EXPLICIT_PAIR_PATTERN = re.compile(
    r"(?:错误(?:识别|写)(?:为|成)|误(?:识别|写)(?:为|成))\s*"
    r"[“\"'‘]?([\u3400-\u9fffA-Za-z0-9·._-]{2,40})[”\"'’]?"
    r"[^。；;\n]{0,40}?"
    r"(?:正确(?:的)?(?:应该)?(?:是|为)|应为|应该是)\s*"
    r"[“\"'‘]?([\u3400-\u9fffA-Za-z0-9·._-]{2,40})[”\"'’]?"
)
_CONFIRMED_TERM_PATTERN = re.compile(
    r"(?:正确(?:的)?(?:公司名|名称|人名|产品名|品牌名|专有名词)?(?:应该)?(?:是|为)"
    r"|正确写法(?:是|为)|应写为|应为)\s*"
    r"[“\"'‘]?([\u3400-\u9fffA-Za-z0-9·._-]{2,40})[”\"'’]?"
)


def normalize_recording_context(value: Any) -> str | None:
    """Return a normalized optional context while preserving free-form paragraphs."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidJobRequest("recording_context must be a string or null.")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    if len(normalized) > MAX_RECORDING_CONTEXT_CHARACTERS:
        raise InvalidJobRequest(
            "recording_context must not exceed 50000 characters."
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise InvalidJobRequest(
            "recording_context contains unsupported control characters."
        )
    return normalized


def recording_context_from_options(options: dict[str, Any]) -> str | None:
    return normalize_recording_context(options.get(RECORDING_CONTEXT_OPTION))


def recording_context_sha256(context: str | None) -> str | None:
    if context is None:
        return None
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def confirmed_term_corrections(
    context: str,
    transcript_texts: list[str],
) -> dict[str, str]:
    """Derive narrow corrections from explicit user confirmations.

    Explicit old/new pairs are accepted directly. When only a confirmed spelling is
    supplied, a repeated same-length one-character transcript variant may be corrected.
    This deliberately avoids treating arbitrary background nouns as replacement rules.
    """

    normalized = normalize_recording_context(context)
    if normalized is None:
        return {}
    corrections: dict[str, str] = {
        old: new
        for old, new in _EXPLICIT_PAIR_PATTERN.findall(normalized)
        if old != new
    }
    confirmed_terms = set(_CONFIRMED_TERM_PATTERN.findall(normalized))
    for term in confirmed_terms:
        repeated_term = term[0] + term
        if any(repeated_term in text for text in transcript_texts):
            corrections.setdefault(repeated_term, term)
        candidates: Counter[str] = Counter()
        width = len(term)
        for text in transcript_texts:
            for start in range(0, max(0, len(text) - width + 1)):
                candidate = text[start : start + width]
                if candidate == term or not all(
                    _TERM_CHARACTERS.fullmatch(character) for character in candidate
                ):
                    continue
                if sum(left != right for left, right in zip(candidate, term)) == 1:
                    candidates[candidate] += 1
        minimum_occurrences = 2 if width <= 3 else 1
        for candidate, count in candidates.items():
            if count >= minimum_occurrences:
                corrections.setdefault(candidate, term)
                repeated_candidate = candidate[0] + candidate
                if any(repeated_candidate in text for text in transcript_texts):
                    corrections.setdefault(repeated_candidate, term)
    return corrections


def apply_text_corrections(value: Any, corrections: dict[str, str]) -> tuple[Any, int]:
    """Recursively correct derived strings without touching immutable ASR records."""

    if isinstance(value, str):
        corrected = value
        count = 0
        for old, new in sorted(corrections.items(), key=lambda item: len(item[0]), reverse=True):
            occurrences = corrected.count(old)
            if occurrences:
                corrected = corrected.replace(old, new)
                count += occurrences
        return corrected, count
    if isinstance(value, list):
        items: list[Any] = []
        count = 0
        for item in value:
            corrected, item_count = apply_text_corrections(item, corrections)
            items.append(corrected)
            count += item_count
        return items, count
    if isinstance(value, dict):
        items: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            corrected, item_count = apply_text_corrections(item, corrections)
            items[key] = corrected
            count += item_count
        return items, count
    return value, 0
