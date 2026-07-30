"""Reference-text quality measurements for controlled ASR probes."""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CharacterErrorReport:
    """Normalized character-level edit distance and error rate."""

    distance: int
    reference_characters: int
    hypothesis_characters: int
    character_error_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_for_character_error_rate(text: str) -> str:
    """Normalize case, width, whitespace, and punctuation for multilingual CER."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())


def measure_character_error_rate(reference: str, hypothesis: str) -> CharacterErrorReport:
    """Calculate Levenshtein CER with bounded-row memory.

    The optional probe reference is intended for short reviewed fixtures. The
    persistent Worker will later evaluate long recordings segment by segment.
    """

    normalized_reference = normalize_for_character_error_rate(reference)
    normalized_hypothesis = normalize_for_character_error_rate(hypothesis)
    if not normalized_reference:
        raise ValueError("reference text must contain at least one letter or number")

    distance = _levenshtein_distance(normalized_reference, normalized_hypothesis)
    return CharacterErrorReport(
        distance=distance,
        reference_characters=len(normalized_reference),
        hypothesis_characters=len(normalized_hypothesis),
        character_error_rate=distance / len(normalized_reference),
    )


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            insertion = current[right_index - 1] + 1
            deletion = previous[right_index] + 1
            substitution = previous[right_index - 1] + (left_character != right_character)
            current.append(min(insertion, deletion, substitution))
        previous = current
    return previous[-1]
