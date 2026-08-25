import { describe, expect, it } from "vitest";

import {
  applyJobAction,
  effectiveSpeakerDisplayName,
  effectiveTranscriptSegment,
  getJobSnapshot,
  decideJobSummaryRevision,
  listJobCorrections,
  listJobSummaryRevisions,
  listJobs,
  regenerateJobSummary,
  saveJobSummaryRevisionDraft,
  renameJobSpeakerDisplayName,
  reviewTranscriptSegment
} from "../src/job-client";
import type { WorkerConnectionSettings } from "../src/settings";
import type {
  WorkerTransport,
  WorkerTransportResponse
} from "../src/worker-probe";

const WORKER: WorkerConnectionSettings = {
  id: "home",
  displayName: "书房 Mac",
  endpoint: "https://worker.example.test",
  kind: "remote"
};
const JOB = {
  job_id: `job_${"a".repeat(32)}`,
  source_display_name: "synthetic.wav",
  state: "transcribing",
  revision: 4,
  recording_date: "2026-08-03"
};

describe("job client", () => {
  it("lists only the authorized Vault and reads progressive snapshot content", async () => {
    const transport = new QueueTransport([
      response(200, { jobs: [JOB] }),
      response(200, {
        job: JOB,
        stable_segments: [
          {
            segment_id: "seg_00000001",
            segment_sequence: 1,
            start_ms: 0,
            end_ms: 5000,
            text: "合成稳定文字"
          }
        ],
        provisional: {
          start_ms: 5000,
          end_ms: 7000,
          text: "合成临时结果"
        },
        progress: {
          stage: "transcribing",
          stage_progress: 0.44,
          duration_ms: 10000,
          processed_ms: 4400,
          detail: {
            substage: "transcript_polish",
            completed_units: 4,
            total_units: 9,
            cache_hits: 2,
            retry_attempt: 0,
            model_id: "qwen3:8b",
            input_tokens: 1200,
            output_tokens: 300
          }
        }
      })
    ]);

    const jobs = await listJobs(transport, WORKER, "secret", "vault_one");
    const snapshot = await getJobSnapshot(
      transport,
      WORKER,
      "secret",
      jobs[0]!.job_id
    );

    expect(transport.requests[0]?.path).toContain("vault_id=vault_one");
    expect(snapshot.stable_segments[0]?.text).toBe("合成稳定文字");
    expect(snapshot.provisional?.text).toBe("合成临时结果");
    expect(snapshot.progress?.detail?.cache_hits).toBe(2);
  });

  it("sends revision-bound idempotent lifecycle actions", async () => {
    const transport = new QueueTransport([
      response(200, { applied: true, job: { ...JOB, state: "paused", revision: 5 } })
    ]);

    const paused = await applyJobAction(
      transport,
      WORKER,
      "secret",
      JOB,
      "pause"
    );

    expect(paused.state).toBe("paused");
    expect(transport.requests[0]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/pause`,
      body: { expected_revision: 4 }
    });
    expect(transport.requests[0]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
  });

  it("reads every transcript page instead of truncating long recordings", async () => {
    const transport = new QueueTransport([
      response(200, snapshotPage([segment(1)], true, 1)),
      response(200, snapshotPage([segment(2)], false, 2))
    ]);

    const snapshot = await getJobSnapshot(
      transport,
      WORKER,
      "secret",
      JOB.job_id
    );

    expect(snapshot.stable_segments.map((item) => item.segment_sequence)).toEqual([
      1,
      2
    ]);
    expect(transport.requests[0]?.path).toContain("segment_limit=500");
    expect(transport.requests[1]?.path).toContain("after_segment_sequence=1");
  });

  it("saves text and speaker attribution together and overlays the ledger", async () => {
    const correction = {
      correction_id: "cor_test",
      job_revision: 5,
      field: "segment_review",
      target_id: "seg_00000001",
      after: JSON.stringify({ speaker_id: null, text: "校订后的文字" })
    };
    const transport = new QueueTransport([
      response(200, { corrections: [correction] }),
      response(200, { created: true, correction, job: { ...JOB, revision: 5 } })
    ]);

    const corrections = await listJobCorrections(
      transport,
      WORKER,
      "secret",
      JOB.job_id
    );
    const reviewed = effectiveTranscriptSegment(
      segment(1) as never,
      corrections as never
    );
    const saved = await reviewTranscriptSegment(
      transport,
      WORKER,
      "secret",
      {
        job: JOB,
        segmentId: "seg_00000001",
        beforeText: "合成文字 1",
        afterText: "校订后的文字",
        beforeSpeakerId: "speaker_0",
        afterSpeakerId: null
      }
    );

    expect(reviewed).toEqual({
      text: "校订后的文字",
      speakerId: null,
      revised: true
    });
    expect(saved.created).toBe(true);
    expect(transport.requests[1]?.body).toMatchObject({
      before_text: "合成文字 1",
      after_text: "校订后的文字",
      before_speaker_id: "speaker_0",
      after_speaker_id: null
    });
  });

  it("renames one anonymous speaker label without changing segment attribution", async () => {
    const correction = {
      correction_id: "cor_speaker_name",
      job_revision: 5,
      field: "speaker_display_name",
      target_id: "speaker_0",
      after: "王总"
    };
    const transport = new QueueTransport([
      response(200, {
        created: true,
        correction,
        job: { ...JOB, revision: 5 }
      })
    ]);

    expect(effectiveSpeakerDisplayName("speaker_0", [])).toEqual({
      displayName: "Speaker 0",
      revised: false
    });
    expect(effectiveSpeakerDisplayName("speaker_0", [correction] as never)).toEqual({
      displayName: "王总",
      revised: true
    });

    const saved = await renameJobSpeakerDisplayName(
      transport,
      WORKER,
      "secret",
      {
        job: JOB,
        speakerId: "speaker_0",
        before: "Speaker 0",
        after: "王总"
      }
    );

    expect(saved.created).toBe(true);
    expect(transport.requests[0]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/speaker-display-name`,
      body: {
        expected_revision: 4,
        speaker_id: "speaker_0",
        before: "Speaker 0",
        after: "王总",
        author: "obsidian-user"
      }
    });
    expect(transport.requests[0]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
    expect(transport.requests[0]?.timeoutMs).toBe(12_000);
  });

  it("lists candidate notes and sends a whole-version decision", async () => {
    const revision = {
      revision_key: "summary_revision_test",
      base_version: 1,
      candidate_version: 2,
      status: "pending",
      changed: true,
      text_correction_count: 2,
      speaker_rename_count: 1,
      before_document: { summary: { text: "旧总览" } },
      after_document: { summary: { text: "新总览" } },
      diff_truncated: false,
      created_at: "2026-08-08T00:00:00Z",
      decided_at: null,
      artifact_manifest_sha256: null,
      draft_markdown: null,
      draft_version: 0,
      draft_updated_at: null,
      draft_sha256: null
    };
    const transport = new QueueTransport([
      response(200, {
        revisions: [revision],
        current_version: 1,
        manual_section_markdown: "## 我的补充\n\n人工内容。\n",
        can_regenerate: false
      }),
      response(200, {
        applied: true,
        job: JOB,
        revision: { ...revision, status: "rejected" }
      })
    ]);

    const listed = await listJobSummaryRevisions(
      transport,
      WORKER,
      "secret",
      JOB.job_id
    );
    const decided = await decideJobSummaryRevision(
      transport,
      WORKER,
      "secret",
      {
        job: JOB,
        revisionKey: revision.revision_key,
        decision: "rejected"
      }
    );

    expect(listed.revisions[0]?.after_document).toEqual({
      summary: { text: "新总览" }
    });
    expect(listed.manual_section_markdown).toContain("人工内容");
    expect(decided.revision.status).toBe("rejected");
    expect(transport.requests[1]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/summary-revisions/${revision.revision_key}/decision`,
      body: { expected_revision: 4, decision: "rejected" }
    });
    expect(transport.requests[1]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
  });

  it("triggers one revision-bound note regeneration", async () => {
    const revision = {
      revision_key: "summary_revision_regenerated",
      base_version: 1,
      candidate_version: 2,
      status: "pending",
      changed: true,
      text_correction_count: 1,
      speaker_rename_count: 0,
      before_document: { summary: { text: "旧总览" } },
      after_document: { summary: { text: "新总览" } },
      diff_truncated: false,
      created_at: "2026-08-08T00:00:00Z",
      decided_at: null,
      artifact_manifest_sha256: null,
      draft_markdown: null,
      draft_version: 0,
      draft_updated_at: null,
      draft_sha256: null
    };
    const transport = new QueueTransport([
      response(200, { applied: true, job: JOB, revision })
    ]);

    const result = await regenerateJobSummary(
      transport,
      WORKER,
      "secret",
      JOB
    );

    expect(result.revision.revision_key).toBe(revision.revision_key);
    expect(transport.requests[0]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/summary-revisions`,
      body: { expected_revision: 4 }
    });
    expect(transport.requests[0]?.headers?.["Idempotency-Key"]).toMatch(
      /^obsidian-[0-9a-f]{64}$/
    );
    expect(transport.requests[0]?.timeoutMs).toBe(60 * 60_000);
  });

  it("saves a version-bound human candidate Note draft", async () => {
    const revision = {
      revision_key: "summary_revision_draft",
      base_version: 1,
      candidate_version: 2,
      status: "pending",
      changed: true,
      text_correction_count: 1,
      speaker_rename_count: 0,
      before_document: { summary: { text: "旧总览" } },
      after_document: { summary: { text: "新总览" } },
      diff_truncated: false,
      created_at: "2026-08-08T00:00:00Z",
      decided_at: null,
      artifact_manifest_sha256: null,
      draft_markdown: "# 人工定稿\n",
      draft_version: 1,
      draft_updated_at: "2026-08-08T00:01:00Z",
      draft_sha256: "a".repeat(64)
    };
    const transport = new QueueTransport([
      response(200, { saved: true, job: JOB, revision })
    ]);

    const result = await saveJobSummaryRevisionDraft(
      transport,
      WORKER,
      "secret",
      {
        job: JOB,
        revisionKey: revision.revision_key,
        expectedDraftVersion: 0,
        markdown: "# 人工定稿\n"
      }
    );

    expect(result.revision.draft_version).toBe(1);
    expect(transport.requests[0]).toMatchObject({
      path: `/v1/jobs/${JOB.job_id}/summary-revisions/${revision.revision_key}/draft`,
      timeoutMs: 30_000,
      body: {
        expected_revision: 4,
        expected_draft_version: 0,
        markdown: "# 人工定稿\n"
      }
    });
  });
});

function segment(sequence: number) {
  return {
    segment_id: `seg_${sequence.toString().padStart(8, "0")}`,
    segment_sequence: sequence,
    start_ms: (sequence - 1) * 5_000,
    end_ms: sequence * 5_000,
    text: `合成文字 ${sequence.toString()}`,
    speaker_id: "speaker_0"
  };
}

function snapshotPage(segments: unknown[], hasMore: boolean, next: number) {
  return {
    job: JOB,
    stable_segments: segments,
    provisional: null,
    progress: null,
    has_more_segments: hasMore,
    next_after_segment_sequence: next
  };
}

function response(status: number, json: unknown): WorkerTransportResponse {
  return { status, json };
}

class QueueTransport implements WorkerTransport {
  public readonly requests: Array<{
    path: string;
    body?: unknown;
    headers?: Readonly<Record<string, string>>;
    timeoutMs?: number;
  }> = [];

  public constructor(private readonly responses: WorkerTransportResponse[]) {}

  public async request(
    _worker: WorkerConnectionSettings,
    path: string,
    options: {
      readonly method?: "GET" | "POST" | "PUT";
      readonly body?: unknown;
      readonly rawBody?: ArrayBuffer;
      readonly bearerToken?: string;
      readonly headers?: Readonly<Record<string, string>>;
      readonly timeoutMs?: number;
    } = {}
  ): Promise<WorkerTransportResponse> {
    this.requests.push({
      path,
      ...(options.body === undefined ? {} : { body: options.body }),
      ...(options.headers === undefined ? {} : { headers: options.headers }),
      ...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs })
    });
    const next = this.responses.shift();
    if (!next) {
      throw new Error("Synthetic response queue exhausted.");
    }
    return next;
  }
}
