from speech_capture_worker.completeness import (
    evaluate_chunk_coverage,
    validate_timestamp_segments,
)


def chunk(index: int, start: float, end: float, text: str = "speech") -> dict:
    return {
        "chunk_index": index,
        "start": start,
        "end": end,
        "text": text,
        "finish_reason": "stop",
        "truncated": False,
    }


def issue_codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def test_contiguous_chunks_are_complete() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 30.0), chunk(1, 30.0, 60.0)],
        source_duration_sec=60.0,
    )

    assert report.complete is True
    assert report.accounted_duration_sec == 60.0
    assert report.issues == ()


def test_small_codec_duration_difference_is_tolerated() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 10.0)],
        source_duration_sec=10.08,
    )

    assert report.complete is True


def test_gap_is_not_complete() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 20.0), chunk(1, 21.0, 40.0)],
        source_duration_sec=40.0,
    )

    assert report.complete is False
    assert "UNCOVERED_RANGE" in issue_codes(report)


def test_overlap_is_not_complete() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 20.0), chunk(1, 19.0, 40.0)],
        source_duration_sec=40.0,
    )

    assert report.complete is False
    assert "OVERLAPPING_RANGE" in issue_codes(report)


def test_truncated_chunk_is_not_complete() -> None:
    first = chunk(0, 0.0, 10.0)
    first["finish_reason"] = "length"
    first["truncated"] = True

    report = evaluate_chunk_coverage([first], source_duration_sec=10.0)

    assert report.complete is False
    assert "TRUNCATED_CHUNK" in issue_codes(report)


def test_empty_chunk_requires_explicit_non_speech_classification() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 10.0, text="")],
        source_duration_sec=10.0,
    )

    assert report.complete is False
    assert "EMPTY_CHUNK_OUTPUT" in issue_codes(report)


def test_duplicate_chunk_index_is_not_complete() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 10.0), chunk(0, 10.0, 20.0)],
        source_duration_sec=20.0,
    )

    assert report.complete is False
    assert "NON_CONTIGUOUS_CHUNK_INDEX" in issue_codes(report)


def test_final_range_must_reach_source_end() -> None:
    report = evaluate_chunk_coverage(
        [chunk(0, 0.0, 9.0)],
        source_duration_sec=10.0,
    )

    assert report.complete is False
    assert "UNCOVERED_RANGE" in issue_codes(report)


def test_timestamp_segments_must_be_monotonic_and_in_bounds() -> None:
    issues = validate_timestamp_segments(
        [
            {"start": 0.5, "end": 1.0, "text": "one"},
            {"start": 0.2, "end": 0.4, "text": "two"},
            {"start": 9.0, "end": 10.5, "text": "three"},
        ],
        source_duration_sec=10.0,
    )

    codes = {issue.code for issue in issues}
    assert "NON_MONOTONIC_TIMESTAMP" in codes
    assert "TIMESTAMP_OUT_OF_BOUNDS" in codes
