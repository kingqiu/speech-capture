export interface SegmentReviewDraft {
  readonly text: string;
  readonly speakerId: string | null;
}

export function segmentReviewDraftKey(jobId: string, segmentId: string): string {
  return `${jobId}:${segmentId}`;
}

export function isSegmentReviewDraftDirty(
  draft: SegmentReviewDraft,
  effective: SegmentReviewDraft
): boolean {
  return (
    draft.text.trim() !== effective.text ||
    draft.speakerId !== effective.speakerId
  );
}
