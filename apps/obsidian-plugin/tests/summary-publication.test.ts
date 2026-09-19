import { describe, expect, it } from "vitest";

import {
  currentPublicationReceipt,
  currentAcceptedSummaryManifest,
  publishedManifestIsStale,
  summaryRevisionIsPublished,
  upsertSavedSummaryRevision
} from "../src/summary-publication";

describe("summary publication transition", () => {
  it("does not treat the V1 receipt as proof that the new V2 manifest is published", () => {
    const status = {
      manifest_sha256: "b".repeat(64),
      receipt: {
        manifest_sha256: "a".repeat(64),
        target_relative_path: "Speech Notes/V1",
        published_at: "2026-08-25T10:00:00Z"
      }
    } as never;

    expect(currentPublicationReceipt(status)).toBeNull();
  });

  it("returns a receipt only when it describes the current manifest", () => {
    const receipt = {
      manifest_sha256: "b".repeat(64),
      target_relative_path: "Speech Notes/V2",
      published_at: "2026-08-26T10:00:00Z"
    };
    const status = {
      manifest_sha256: receipt.manifest_sha256,
      receipt
    } as never;

    expect(currentPublicationReceipt(status)).toEqual(receipt);
  });

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

  it("recognizes only the accepted revision represented by the current receipt", () => {
    const revision = {
      status: "accepted",
      artifact_manifest_sha256: "b".repeat(64)
    } as never;

    expect(summaryRevisionIsPublished(revision, "b".repeat(64))).toBe(true);
    expect(summaryRevisionIsPublished(revision, "a".repeat(64))).toBe(false);
    expect(summaryRevisionIsPublished(revision, null)).toBe(false);
  });

  it("adds a forked V3 returned while editing a published V2", () => {
    const v2 = {
      revision_key: "revision_00000002",
      candidate_version: 2,
      status: "accepted"
    };
    const v3 = {
      revision_key: "revision_manual_00000003",
      candidate_version: 3,
      status: "accepted"
    };
    const updated = upsertSavedSummaryRevision(
      {
        current_version: 2,
        revisions: [v2]
      } as never,
      v3 as never
    );

    expect(updated.current_version).toBe(3);
    expect(updated.revisions).toHaveLength(2);
    expect(updated.revisions[1]?.revision_key).toBe("revision_manual_00000003");
  });
});
