import { describe, expect, it } from "vitest";

import type { JobSchema } from "../../../packages/protocol/generated/typescript/speech-capture-protocol";
import { sameJobListPresentation } from "../src/job-list-refresh";

function job(overrides: Partial<JobSchema> = {}): JobSchema {
  return {
    content_type_override: null,
    created_at: "2026-08-09T08:00:00Z",
    job_id: "job-1",
    language_hint: null,
    last_error_code: null,
    last_error_message: null,
    model_profile: "accuracy",
    recording_context: null,
    recording_date: "2026-08-09",
    revision: 1,
    source_display_name: "访谈.wav",
    source_audio_deleted_at: null,
    source_audio_deleted_bytes: 0,
    source_audio_status: "available",
    source_sha256: "a".repeat(64),
    source_size_bytes: 1024,
    source_upload_id: null,
    state: "queued",
    updated_at: "2026-08-09T08:00:00Z",
    vault_id: "vault-test",
    ...overrides
  };
}

describe("sameJobListPresentation", () => {
  it("keeps an unchanged poll from forcing a workbench rerender", () => {
    expect(sameJobListPresentation([job()], [{ ...job() }])).toBe(true);
  });

  it("detects changes that affect task cards", () => {
    expect(
      sameJobListPresentation([job()], [
        job({ state: "transcribing", revision: 2, updated_at: "2026-08-09T08:00:03Z" })
      ])
    ).toBe(false);
    expect(
      sameJobListPresentation([job()], [job({ source_display_name: "会议.wav" })])
    ).toBe(false);
    expect(sameJobListPresentation([job()], [])).toBe(false);
  });
});
