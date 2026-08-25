import type {
  SummaryRevisionListResponse
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

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
