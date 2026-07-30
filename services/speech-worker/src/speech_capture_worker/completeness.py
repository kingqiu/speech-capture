"""Timeline completeness checks for ASR chunk output."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CoverageIssue:
    """A machine-readable reason why a transcript cannot be called complete."""

    code: str
    message: str
    start_sec: float | None = None
    end_sec: float | None = None


@dataclass(frozen=True)
class CoverageReport:
    """Result of comparing ASR chunk coverage with the decoded source timeline."""

    complete: bool
    source_duration_sec: float
    accounted_duration_sec: float
    chunk_count: int
    issues: tuple[CoverageIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "source_duration_sec": self.source_duration_sec,
            "accounted_duration_sec": self.accounted_duration_sec,
            "chunk_count": self.chunk_count,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class _Chunk:
    index: int
    start_sec: float
    end_sec: float
    text: str
    finish_reason: str | None
    truncated: bool


def evaluate_chunk_coverage(
    chunks: Iterable[dict[str, Any]] | None,
    *,
    source_duration_sec: float,
    tolerance_sec: float = 0.25,
) -> CoverageReport:
    """Require a continuous, non-truncated outcome for the decoded timeline.

    An empty chunk is unresolved in the spike because V1 has not yet added a
    separate silence/non-speech classifier. This is intentionally conservative:
    an empty model response cannot silently pass as complete.
    """

    if not math.isfinite(source_duration_sec) or source_duration_sec <= 0:
        raise ValueError("source_duration_sec must be a positive finite number")
    if not math.isfinite(tolerance_sec) or tolerance_sec < 0:
        raise ValueError("tolerance_sec must be a non-negative finite number")

    issues: list[CoverageIssue] = []
    normalized: list[_Chunk] = []
    for position, raw in enumerate(chunks or []):
        try:
            index = int(raw.get("chunk_index", position))
            start_sec = float(raw["start"])
            end_sec = float(raw["end"])
        except (KeyError, TypeError, ValueError):
            issues.append(
                CoverageIssue(
                    code="INVALID_CHUNK",
                    message=f"Chunk at position {position} has invalid timing metadata.",
                )
            )
            continue

        if not all(math.isfinite(value) for value in (start_sec, end_sec)):
            issues.append(
                CoverageIssue(
                    code="INVALID_CHUNK",
                    message=f"Chunk {index} has non-finite timing metadata.",
                )
            )
            continue
        if end_sec <= start_sec:
            issues.append(
                CoverageIssue(
                    code="INVALID_CHUNK_RANGE",
                    message=f"Chunk {index} does not have a positive duration.",
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
            continue

        normalized.append(
            _Chunk(
                index=index,
                start_sec=start_sec,
                end_sec=end_sec,
                text=str(raw.get("text", "")).strip(),
                finish_reason=_optional_string(raw.get("finish_reason")),
                truncated=bool(raw.get("truncated", False)),
            )
        )

    if not normalized:
        issues.append(
            CoverageIssue(
                code="NO_CHUNKS",
                message="ASR returned no valid chunk records for the source.",
                start_sec=0.0,
                end_sec=source_duration_sec,
            )
        )
        return CoverageReport(
            complete=False,
            source_duration_sec=source_duration_sec,
            accounted_duration_sec=0.0,
            chunk_count=0,
            issues=tuple(issues),
        )

    normalized.sort(key=lambda chunk: (chunk.start_sec, chunk.end_sec, chunk.index))
    actual_indices = sorted(chunk.index for chunk in normalized)
    expected_indices = list(range(len(normalized)))
    if actual_indices != expected_indices:
        issues.append(
            CoverageIssue(
                code="NON_CONTIGUOUS_CHUNK_INDEX",
                message=(
                    "Chunk indices are not a unique zero-based sequence: "
                    f"expected {expected_indices}, got {actual_indices}."
                ),
            )
        )

    cursor = 0.0
    accounted_duration_sec = 0.0
    for chunk in normalized:
        if chunk.start_sec > cursor + tolerance_sec:
            issues.append(
                CoverageIssue(
                    code="UNCOVERED_RANGE",
                    message=f"Audio before chunk {chunk.index} has no processing outcome.",
                    start_sec=cursor,
                    end_sec=chunk.start_sec,
                )
            )
        elif chunk.start_sec < cursor - tolerance_sec:
            issues.append(
                CoverageIssue(
                    code="OVERLAPPING_RANGE",
                    message=f"Chunk {chunk.index} overlaps the previous processed range.",
                    start_sec=chunk.start_sec,
                    end_sec=cursor,
                )
            )

        union_start = max(cursor, chunk.start_sec)
        if chunk.end_sec > union_start:
            accounted_duration_sec += chunk.end_sec - union_start
        cursor = max(cursor, chunk.end_sec)

        if chunk.truncated or chunk.finish_reason == "length":
            issues.append(
                CoverageIssue(
                    code="TRUNCATED_CHUNK",
                    message=f"Chunk {chunk.index} exhausted its generation limit.",
                    start_sec=chunk.start_sec,
                    end_sec=chunk.end_sec,
                )
            )
        if chunk.finish_reason is None:
            issues.append(
                CoverageIssue(
                    code="MISSING_FINISH_REASON",
                    message=f"Chunk {chunk.index} has no generation finish reason.",
                    start_sec=chunk.start_sec,
                    end_sec=chunk.end_sec,
                )
            )
        if not chunk.text:
            issues.append(
                CoverageIssue(
                    code="EMPTY_CHUNK_OUTPUT",
                    message=(
                        f"Chunk {chunk.index} returned no text and has not yet been "
                        "classified as silence or non-speech."
                    ),
                    start_sec=chunk.start_sec,
                    end_sec=chunk.end_sec,
                )
            )

    if normalized[0].start_sec > tolerance_sec:
        # The first gap is already emitted by the loop, so no second issue is needed.
        pass
    elif normalized[0].start_sec < -tolerance_sec:
        issues.append(
            CoverageIssue(
                code="OUT_OF_BOUNDS_RANGE",
                message="The first chunk begins before the source timeline.",
                start_sec=normalized[0].start_sec,
                end_sec=0.0,
            )
        )

    if cursor < source_duration_sec - tolerance_sec:
        issues.append(
            CoverageIssue(
                code="UNCOVERED_RANGE",
                message="The final source range has no processing outcome.",
                start_sec=cursor,
                end_sec=source_duration_sec,
            )
        )
    elif cursor > source_duration_sec + tolerance_sec:
        issues.append(
            CoverageIssue(
                code="OUT_OF_BOUNDS_RANGE",
                message="ASR chunk coverage extends beyond the decoded source duration.",
                start_sec=source_duration_sec,
                end_sec=cursor,
            )
        )

    accounted_duration_sec = min(accounted_duration_sec, source_duration_sec)
    return CoverageReport(
        complete=not issues,
        source_duration_sec=source_duration_sec,
        accounted_duration_sec=accounted_duration_sec,
        chunk_count=len(normalized),
        issues=tuple(issues),
    )


def validate_timestamp_segments(
    segments: Iterable[dict[str, Any]] | None,
    *,
    source_duration_sec: float,
    tolerance_sec: float = 0.25,
) -> tuple[CoverageIssue, ...]:
    """Validate monotonic word or phrase timestamps without requiring silence coverage."""

    issues: list[CoverageIssue] = []
    cursor = 0.0
    for position, segment in enumerate(segments or []):
        try:
            start_sec = float(segment["start"])
            end_sec = float(segment["end"])
        except (KeyError, TypeError, ValueError):
            issues.append(
                CoverageIssue(
                    code="INVALID_TIMESTAMP",
                    message=f"Timestamp segment {position} has invalid timing metadata.",
                )
            )
            continue

        if not all(math.isfinite(value) for value in (start_sec, end_sec)):
            issues.append(
                CoverageIssue(
                    code="INVALID_TIMESTAMP",
                    message=f"Timestamp segment {position} has non-finite timing metadata.",
                )
            )
            continue
        if end_sec < start_sec:
            issues.append(
                CoverageIssue(
                    code="REVERSED_TIMESTAMP",
                    message=f"Timestamp segment {position} ends before it starts.",
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
        if start_sec < cursor - tolerance_sec:
            issues.append(
                CoverageIssue(
                    code="NON_MONOTONIC_TIMESTAMP",
                    message=f"Timestamp segment {position} moves backwards.",
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
        if start_sec < -tolerance_sec or end_sec > source_duration_sec + tolerance_sec:
            issues.append(
                CoverageIssue(
                    code="TIMESTAMP_OUT_OF_BOUNDS",
                    message=f"Timestamp segment {position} is outside the source timeline.",
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
        cursor = max(cursor, end_sec)
    return tuple(issues)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
