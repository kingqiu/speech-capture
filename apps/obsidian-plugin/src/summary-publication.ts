import type {
  PublicationReceiptSchema,
  PublicationStatusResponse,
  SummaryRevisionListResponse,
  SummaryRevisionSchema
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

export function currentPublicationReceipt(
  status: PublicationStatusResponse
): PublicationReceiptSchema | null {
  return status.receipt?.manifest_sha256 === status.manifest_sha256
    ? status.receipt
    : null;
}

export function currentAcceptedSummaryManifest(
  summaryRevisions: SummaryRevisionListResponse | null
): string | null {
  const accepted = [...(summaryRevisions?.revisions ?? [])]
    .reverse()
    .find(
      (revision) =>
        revision.status === "accepted" &&
        revision.artifact_manifest_sha256 !== null
    );
  return accepted?.artifact_manifest_sha256 ?? null;
}

export function publishedManifestIsStale(
  publishedManifestSha256: string,
  summaryRevisions: SummaryRevisionListResponse | null
): boolean {
  const acceptedManifest = currentAcceptedSummaryManifest(summaryRevisions);
  return acceptedManifest !== null && acceptedManifest !== publishedManifestSha256;
}

export function summaryRevisionIsPublished(
  revision: SummaryRevisionSchema,
  publishedManifestSha256: string | null
): boolean {
  return (
    revision.status === "accepted" &&
    revision.artifact_manifest_sha256 !== null &&
    revision.artifact_manifest_sha256 === publishedManifestSha256
  );
}

export function upsertSavedSummaryRevision(
  collection: SummaryRevisionListResponse,
  revision: SummaryRevisionSchema
): SummaryRevisionListResponse {
  const exists = collection.revisions.some(
    (item) => item.revision_key === revision.revision_key
  );
  return {
    ...collection,
    current_version:
      revision.status === "accepted"
        ? Math.max(collection.current_version, revision.candidate_version)
        : collection.current_version,
    revisions: exists
      ? collection.revisions.map((item) =>
          item.revision_key === revision.revision_key ? revision : item
        )
      : [...collection.revisions, revision]
  };
}
