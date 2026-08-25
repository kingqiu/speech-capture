import { describe, expect, it } from "vitest";

import {
  currentAcceptedSummaryManifest,
  publishedManifestIsStale
} from "../src/summary-publication";

describe("summary publication transition", () => {
  it("invalidates an old publication after a candidate creates a new artifact package", () => {
    const revisions = {
      revisions: [
        {
          status: "accepted",
          artifact_manifest_sha256: "b".repeat(64)
        }
      ]
    } as never;

    expect(currentAcceptedSummaryManifest(revisions)).toBe("b".repeat(64));
    expect(publishedManifestIsStale("a".repeat(64), revisions)).toBe(true);
    expect(publishedManifestIsStale("b".repeat(64), revisions)).toBe(false);
  });

  it("does not invalidate publication for pending candidates without artifacts", () => {
    const revisions = {
      revisions: [
        {
          status: "pending",
          artifact_manifest_sha256: null
        }
      ]
    } as never;

    expect(currentAcceptedSummaryManifest(revisions)).toBeNull();
    expect(publishedManifestIsStale("a".repeat(64), revisions)).toBe(false);
  });
});
