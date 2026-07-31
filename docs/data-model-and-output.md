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
        transcript.md
        speech-record.json
        transcript.raw.json
        source/
          original.m4a
  Undated/
    source-title--sp_01J.../
      ...
```

Rules:

- `Work/Speech Notes/` is the personal default, not a hardcoded product requirement.
- `_Tasks/` contains lightweight submission and processing records. It is not the evidence archive.
- The date directory represents the recording date, not the import or processing date.
- `source/` is absent unless the user explicitly enables source archiving.
- The deterministic `speech_id` suffix prevents collisions and survives title changes.
- Publication writes to a temporary sibling directory, verifies hashes, and renames atomically.

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

The Worker core already enforces this attempt-level boundary before final package rendering: each normalized-audio chunk attempt has an immutable private JSON file, SHA-256, model and range metadata, and a one-based attempt number. Visible stable segments are materialized only after the corresponding raw file is durable. The publication layer will assemble those private attempt files into the versioned `transcript.raw.json` artifact without rewriting prior attempts.

### 4.2 Evidence transcript

`transcript.md` is the readable, correctable record.

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

`note.md` is the primary reading surface. It is concise, content-type aware, and evidence-linked.

Generated sections can be refreshed from the reviewed transcript. User-owned sections are protected.

## 5. Main note structure

Every note contains a common core:

```text
Properties
Title and status
One-minute overview
Key information
Evidence-linked details
People and terminology
Uncertainties and omissions
Source and processing provenance
My additions
```

Content-specific sections are then added:

| Content type | Specialized sections |
| --- | --- |
| Meeting | decisions, action items, owners, deadlines, disagreements, unresolved questions |
| Interview | themes, questions and answers, notable claims, tensions, follow-ups |
| Course | concepts, definitions, examples, methods, study questions |
| Speech | thesis, supporting arguments, examples, rhetorical structure |
| Voice memo | ideas, assumptions, next steps, related-note suggestions |
| Generic | chronology, topics, claims, open questions |

Empty specialized sections are omitted rather than filled with generic prose.

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

## 9. Corrections and revisions

Corrections are an append-only ledger:

```json
{
  "correction_id": "cor_01J...",
  "segment_id": "seg_000123",
  "field": "text",
  "before": "original reviewed value",
  "after": "corrected value",
  "author": "user",
  "created_at": "2026-07-30T11:00:00+08:00"
}
```

Supported corrections include:

- transcript text;
- speaker display name;
- speaker attribution;
- paragraph boundaries;
- terminology;
- recording date;
- content type.

Re-running ASR creates a new attempt. It never destroys the prior raw attempt.

## 10. Protected human content

The Markdown renderer owns only clearly marked generated sections. Human content remains outside those regions.

```markdown
<!-- speech-capture:generated:summary:start -->
Generated content
<!-- speech-capture:generated:summary:end -->

## 我的补充

This section belongs to the user and is never replaced by regeneration.
```

If the renderer finds edited generated text whose source revision no longer matches, it creates a conflict revision and asks the user to choose rather than overwriting it.

## 11. Evidence rules for summaries

- Decisions, action items, deadlines, names, numbers, and important factual claims require one or more segment references.
- A generated item without evidence is labeled `unsupported` and excluded from the main summary by default.
- Inference is allowed only when labeled as inference and connected to supporting evidence.
- Disagreement and uncertainty remain visible instead of being normalized into false consensus.
- The summary quality check examines every transcript chunk, not only the beginning or a sampled excerpt.

## 12. Future extension

Interview-intent analysis may add optional records such as inferred intent, conversational tactic, and evidence. Those fields are reserved for a later schema version and are not produced in V1.
