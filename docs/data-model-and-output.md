# Data Model and Output Package

## 1. Design goal

Speech Capture keeps evidence, corrections, interpretation, and presentation separate.

This separation allows a person or future agent to answer four different questions:

1. What did the ASR model originally return?
2. What is the current reviewed transcript?
3. What conclusions were derived from it?
4. What did a person add or change later?

No generated summary is treated as a substitute for the transcript.

## 2. Vault-relative layout

The default root is configurable and uses a Vault-relative path:

```text
Work/Speech Notes/
  _Tasks/
    <speech_id>.md
  2026/
    07/
      2026-07-21-project-review--sp_01J.../
        note.md
        timeline.md
        transcript.md
        note.evidence.md
        speech-record.json
        transcript.raw.json
        artifact-manifest.json
  Undated/
    source-title--sp_01J.../
      ...
```

Rules:

- `Work/Speech Notes/` is the personal default, not a hardcoded product requirement.
- `_Tasks/` contains lightweight submission and processing records. It is not the evidence archive.
- The date directory represents the recording date, not the import or processing date.
- Source audio is not part of the default published package.
- The low-bitrate review-audio copy remains private Worker job data and is not part of the published package.
- The deterministic `speech_id` suffix prevents collisions and survives title changes.
- Publication writes to a temporary sibling directory, verifies hashes, and renames atomically.

### 2.1 Private review audio

During preprocessing, the Worker derives an 8 kHz, 8-bit, mono PCM review WAV from the checksum-bound normalized
audio. It preserves the same start time and duration within 1 millisecond, carries no source metadata, has its own
SHA-256 checkpoint, and remains under the private job directory. Authorized clients may read it with HTTP byte
ranges for evidence seeking. Its current retention is the Worker job lifetime; deleting or expiring a job must also
remove this private derivative. It is never copied to the Vault unless a future explicit audio-archive feature is
separately designed and enabled.

## 3. Date resolution

Recording date and time are resolved in this order:

1. explicit user input;
2. trustworthy embedded media metadata such as `creation_time`;
3. a date and time parsed from the filename;
4. filesystem birth time, shown as a suggestion rather than silently trusted;
5. `Undated`.

The package records these separately:

- `recorded_at`;
- `recorded_at_source`;
- `imported_at`;
- `processed_at`;
- `published_at`.

Changing a confirmed recording date may move the package, but it does not change `speech_id`.

## 4. Four persistent layers

### 4.1 Immutable raw response

`transcript.raw.json` stores the original ASR and timing payload for every attempt.

- It is append-only by attempt.
- It is never rewritten after human correction.
- It includes model, prompt, vocabulary, decoding, and source-range provenance.
- It may be large and is not optimized for direct reading.

The Worker core enforces this attempt-level boundary before final package rendering: each normalized-audio chunk attempt has an immutable private JSON file, SHA-256, model and range metadata, and a one-based attempt number. Visible stable segments are materialized only after the corresponding raw file is durable. Artifact generation assembles those private attempts into versioned `transcript.raw.json` without rewriting prior attempts.

### 4.2 Evidence transcript

`transcript.md` is the readable, correctable record.

The human-facing transcript uses a faithful editorial layer for punctuation and obvious
speech disfluency cleanup. The original ASR text remains available as `raw_text` in
`speech-record.json`. Non-speech ranges and clear sub-second recognition hallucinations are
kept in machine evidence but omitted from the reading surface; significant inaudible ranges
remain explicit.

Each stable segment contains:

- start and end time;
- anonymous or confirmed speaker label;
- reviewed text;
- a stable Obsidian block ID;
- an explicit warning when the segment is uncertain, inaudible, or failed.

Example:

```markdown
## 00:12:40–00:13:18 · Speaker 2

We should finish the migration plan before Friday and ask Lin to review it.
^sp-01j-seg-000123
```

Stable IDs are never derived from paragraph text. Editing text does not break evidence links.

### 4.3 Structured record

`speech-record.json` is the canonical machine-readable interpretation. It contains typed fields for content classification, participants, findings, evidence links, quality, corrections, and provenance.

The JSON record drives deterministic Markdown rendering. Future agents should prefer this record plus the evidence transcript over scraping headings from `note.md`.

### 4.4 Human-facing note

`note.md` is the primary clean reading surface. It is concise and content-type aware, without
inline evidence links. `note.evidence.md` carries the matching auditable links, while `timeline.md`
summarizes the complete corrected transcript in recording order.

Generated sections can be refreshed from the reviewed transcript. User-owned sections are protected.

## 5. Main note structure

Every note contains a content-first common core:

```text
Properties
Title
Narrative content summary
Core conclusions
Thematic discussion with evidence-linked details
Decisions and action items when present
Risks and unresolved questions when present
Time-ordered chapter navigation
Recording information and folded uncertainties
My additions
```

Content-specific sections are then added:

| Content type | Specialized sections |
| --- | --- |
| Meeting | decisions, action items, owners, deadlines, disagreements, unresolved questions |
| Interview | interviewee background, question-answer links, viewpoints, reasoning, experiences, tensions, unanswered questions |
| Course | learning goals, concepts, principles, methods, examples, limitations, review takeaways |
| Speech | theme, arguments, evidence, examples, implications, takeaways |
| Voice memo | intent, ideas, judgments, tasks, hypotheses, constraints, follow-ups |
| Generic | context, themes, insights, details, outcomes, actions, open questions |

Empty specialized sections are omitted rather than filled with generic prose.
These scene sections use a stable `kind`, title, summary, details, and transcript evidence.
They share a quality bar but not a meeting-note layout.

The structured record also distinguishes an automatically detected content type from an optional
user override. If the selected type changes after processing, the Worker keeps raw ASR and the
corrected transcript unchanged, re-extracts type-dependent findings, and then re-synthesizes the
Note. `speech-record.json.content.source` and `automatic_type` preserve this provenance.

## 6. Obsidian properties

The rendered note uses stable, queryable properties:

```yaml
---
speech_id: sp_01J...
status: published
content_type: meeting
recorded_at: 2026-07-21T14:30:00+08:00
recorded_at_source: user_confirmed
duration_seconds: 4621.4
language:
  - zh
  - en
speakers:
  - speaker_id: speaker_01
    display_name: A
worker_id: worker_01
artifact_schema_version: 1.0.0
tags:
  - speech-capture
---
```

Private endpoint URLs, tokens, absolute source paths, and personal device identifiers are never placed in properties.

## 7. Record schema direction

The initial schema reserves these top-level fields:

```json
{
  "schema_version": "1.0.0",
  "speech_id": "sp_01J...",
  "revision": 3,
  "status": "complete",
  "content": {
    "type": "meeting",
    "traits": ["multi_speaker", "action_oriented"],
    "confidence": 0.91
  },
  "source": {
    "display_name": "project-review.m4a",
    "sha256": "<checksum>",
    "duration_ms": 4621400,
    "decodable_ranges": [{"start_ms": 0, "end_ms": 4621400}],
    "unresolved_ranges": []
  },
  "dates": {
    "recorded_at": "2026-07-21T14:30:00+08:00",
    "recorded_at_source": "user_confirmed",
    "imported_at": "2026-07-30T10:00:00+08:00"
  },
  "segments": [
    {
      "segment_id": "seg_000123",
      "start_ms": 760000,
      "end_ms": 798000,
      "speaker_id": "speaker_02",
      "text_revision": 2,
      "block_id": "sp-01j-seg-000123",
      "quality": "reviewed"
    }
  ],
  "findings": [
    {
      "finding_id": "finding_0042",
      "kind": "action_item",
      "text": "Complete the migration plan before Friday.",
      "evidence": ["seg_000123"],
      "confidence": 0.94,
      "review_state": "generated"
    }
  ],
  "corrections": [],
  "provenance": {},
  "quality": {}
}
```

The implementation will publish versioned JSON Schemas before clients rely on these fields.

## 8. Complete-content accounting

The media timeline is partitioned into explicit ranges:

- decoded and transcribed;
- decoded but marked inaudible;
- decoded but failed after retry;
- non-audio or silence;
- undecodable source region.

A job cannot be marked `complete` if any decodable range has no explicit outcome,
or if an explicit outcome is `inaudible` or `failed`.

`complete` means the entire validated timeline is accounted for without an
inaudible or failed range. `partial` means one or more ranges remain unresolved,
inaudible, or failed, with exact ranges and stable reasons present in the
package.

An exact unresolved range may be accounted for by explicit human review as
`non_speech` or `inaudible`. That evidence is version-bound and idempotent, and
contains no reviewer identity or free-form note. A reviewed `inaudible` range
remains a partial transcript even though the timeline itself is fully
accounted for.

This rule detects silent truncation even when the ASR process exits successfully.

When a stable transcribed segment initially has only estimated timing, forced
alignment may revise its outer start and end without changing its ID, commit
key, text, language, or speaker state. The revised `aligned` status is accepted
by the completeness gate only while matching checksummed private word evidence
remains available and valid.

## 9. Corrections and revisions

Corrections are an append-only ledger:

```json
{
  "correction_id": "cor_01J...",
  "job_revision": 13,
  "field": "transcript_text",
  "target_id": "seg_000123",
  "before": "original reviewed value",
  "after": "corrected value",
  "author": "user",
  "created_at": "2026-07-30T11:00:00+08:00"
}
```

Worker schema 6 implements these first three correction fields:

- transcript text;
- speaker display name;
- recording date;

Speaker attribution, paragraph boundaries, terminology as a separate field, and content type
remain later extensions. Content type already has its own revision-guarded job override.

Each correction is bound to one job revision and one idempotency key. Replaying the same request
returns the existing ledger row; reusing its key for different values fails. A stale revision or a
`before` value that no longer matches the latest correction also fails. Corrections are accepted
only for a processed job. Transcript targets and speaker targets must already exist in that job.

Artifact schema `1.6.0` overlays the ordered ledger on derived values. `transcript.md`, the display
`text` in `speech-record.json`, speaker display names, and recording-date metadata can change, while
database transcript text, `raw_text`, and `transcript.raw.json` remain immutable. The artifact
checkpoint fingerprints the ledger, so a new correction triggers normal deterministic regeneration
without requiring `--force`.

Summary-only regeneration reads the complete corrected transcript, reuses durable extracted
findings, and does not rerun ASR, transcript polishing, or extraction batches. Every such run stores
a private `summary_revisions` checkpoint containing before/after hashes and a bounded unified JSON
diff. This comparison is review state, not a seventh user-facing Markdown document. The six-file
artifact package remains unchanged.

Re-running ASR creates a new attempt. It never destroys the prior raw attempt.

## 10. Protected human content

Starting in artifact schema `1.5.0`, the clean `note.md` ends with one terminal user-owned section:

```markdown
## 我的补充

This section belongs to the user and is never replaced by regeneration.
```

The renderer replaces only the machine-generated Markdown before this heading. It preserves the heading and every byte through EOF without interpreting nested headings, tasks, wikilinks, callouts, or whitespace. `note.evidence.md` is fully machine-maintained and never contains this manual section.

Before treating an artifact checkpoint as idempotent, the Worker verifies all six generated file hashes. A manual edit therefore triggers deterministic regeneration and a new manifest hash while preserving the terminal section. If an existing clean Note is not UTF-8, is not a regular file, exceeds the safety limit, is a symlink, or lacks the terminal heading, regeneration fails before writing any artifact. This is intentionally safer than guessing which content belongs to the user.

Vault publication verifies all six artifacts and the manifest before claiming publication. It writes
the complete seven-file package to a temporary sibling directory, verifies it, and atomically renames
the directory. A pre-existing target is accepted only when every expected file matches; user edits,
extra files, symlinks, path escape, or checksum differences are preserved and reported as conflicts.

## 11. Evidence rules for summaries

- Decisions, action items, deadlines, names, numbers, and important factual claims require one or more segment references.
- A generated item without evidence is labeled `unsupported` and excluded from the main summary by default.
- Inference is allowed only when labeled as inference and connected to supporting evidence.
- Disagreement and uncertainty remain visible instead of being normalized into false consensus.
- The summary quality check examines every transcript chunk, not only the beginning or a sampled excerpt.

## 12. Future extension

Interview-intent analysis may add optional records such as inferred intent, conversational tactic, and evidence. Those fields are reserved for a later schema version and are not produced in V1.
