import { describe, expect, it } from "vitest";

import {
  isSegmentReviewDraftDirty,
  segmentReviewDraftKey
} from "../src/review-draft";

describe("review draft helpers", () => {
  it("scopes drafts by job and segment", () => {
    expect(segmentReviewDraftKey("job_a", "seg_1")).toBe("job_a:seg_1");
    expect(segmentReviewDraftKey("job_b", "seg_1")).not.toBe(
      segmentReviewDraftKey("job_a", "seg_1")
    );
  });

  it("keeps meaningful text and speaker changes dirty across rerenders", () => {
    const effective = { text: "原始校订文字", speakerId: "speaker_1" };

    expect(
      isSegmentReviewDraftDirty(
        { text: "尚未保存的新文字", speakerId: "speaker_1" },
        effective
      )
    ).toBe(true);
    expect(
      isSegmentReviewDraftDirty(
        { text: "原始校订文字", speakerId: "speaker_2" },
        effective
      )
    ).toBe(true);
    expect(
      isSegmentReviewDraftDirty(
        { text: " 原始校订文字 ", speakerId: "speaker_1" },
        effective
      )
    ).toBe(false);
  });
});
