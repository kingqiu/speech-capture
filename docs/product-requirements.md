# Product Requirements

## 1. Product summary

Speech Capture turns large audio files into a durable knowledge package inside Obsidian:

1. a complete evidence-preserving transcript;
2. a readable structured note;
3. a machine-readable record for future agents.

The product is local-first. A user-owned Apple Silicon Mac performs transcription, speaker diarization, and summarization. Another Obsidian desktop can submit work to that Mac without opening a remote desktop session.

## 2. Primary user outcomes

The product succeeds only when both outcomes are met:

- **Content preservation:** all decodable audio content is accounted for and the original model output remains available.
- **Information usefulness:** summaries capture important facts, decisions, actions, arguments, and open questions without inventing unsupported claims.

## 3. Target scenarios

### 3.1 Same-device processing

A user selects an audio file from the active Vault, an external file picker, drag and drop, or an embedded audio link. The plugin submits it to a Worker on the same Mac.

### 3.2 Remote processing

A user traveling with a laptop submits a large audio file from Obsidian to a paired Worker on a home Mac. The laptop may close after the upload is accepted. The Worker continues and keeps the result until a Vault publisher is available.

### 3.3 Multiple content types

The system automatically classifies the material, while allowing a manual override before or after processing:

- meeting or work discussion;
- interview, dialogue, podcast, or roundtable;
- course or lecture;
- speech or monologue;
- voice memo or idea capture;
- generic fallback.

Classification also records traits such as multi-speaker, question-and-answer, technical, and action-oriented.

### 3.4 Multi-speaker material

The system distinguishes anonymous speakers within a recording. Users confirm real names. V1 does not perform cross-recording biometric identity recognition.

## 4. V1 scope

### 4.1 Inputs

- Vault audio file context menu.
- External file picker.
- Drag and drop.
- Audio embedded in the active note.
- Common formats supported through FFmpeg, including WAV, MP3, M4A, FLAC, and audio tracks from supported media containers.

The original source file is never modified or deleted by Speech Capture.

### 4.2 Upload and intake

- Resumable, chunked upload for remote files.
- Source size and whole-file checksum recorded before acceptance.
- Missing chunks can be resumed without restarting the upload.
- Formal transcription starts only after the complete file is received, checksummed, and decoded successfully.
- No arbitrary V1 duration limit; resource and disk preflight determine whether a job can start.
- Duplicate source hashes prompt the user to reuse an existing result or create a new processing attempt.

### 4.3 Local model pipeline

- Accuracy-first ASR profile: Qwen3-ASR 1.7B through `mlx-qwen3-asr`.
- Speed profile: Qwen3-ASR 0.6B.
- Word or phrase timestamps through the supported forced aligner.
- Local speaker diarization through pyannote integration.
- Local hierarchical summarization through Ollama.
- Default summary model profile: Qwen3 14B.
- Lower-resource summary profile: Qwen3 8B.
- Automatic language detection with an optional per-job language hint.

### 4.4 Language behavior

- The transcript preserves the spoken language.
- Chinese and English code-switching remains code-switched.
- The structured note uses a user-configured output language; the personal V1 default is Simplified Chinese.
- Proper nouns, product names, and necessary quotations retain their original form.
- Full translation is a separate optional artifact and is disabled by default.

### 4.5 Optional recording context

- Submission provides one optional free-form background field without requiring a fixed
  sentence count, paragraph shape, participant list, or other structured form.
- The user may provide incomplete context such as the topic, organization names,
  participant relationships, domain terms, or anything else they happen to know.
- Raw ASR evidence is produced and preserved independently. The supplied context is used
  only during downstream transcript cleanup, content interpretation, and structured-note
  synthesis.
- Supplied context is a reference, not transcript evidence. It may help normalize a
  phonetically compatible proper noun, but it cannot create a decision, action, claim, or
  other meeting fact that is not supported by the transcript.
- When supplied context conflicts with transcript evidence, the pipeline preserves the
  evidence, avoids forced replacement, and keeps unresolved ambiguity explicit.
- V1 does not require users to build or maintain a Vault-wide terminology dictionary.
  A terminology-learning workflow remains a later quality extension.

### 4.6 Structured-note quality baseline

- Generation 19 of the validated real meeting sample is the V1 human-approved quality
  reference for clarity, information coverage, factual accuracy, and evidence traceability.
- Every supported content type—meeting, interview, course or speech, voice memo or
  personal note, and generic content—must reach the same quality level. This is a shared
  quality bar, not a requirement to reuse the meeting-note headings or layout.
- Meetings prioritize context, participants, viewpoints, discussion evolution, decisions,
  actions, risks, and open questions.
- Interviews distinguish interviewer prompts from interviewee statements and preserve the
  interviewee's background, central views, reasoning, examples, tensions, and unanswered
  questions.
- Courses and speeches preserve the topic hierarchy, concepts, arguments, examples,
  constraints, and reviewable takeaways.
- Voice memos and personal notes preserve the speaker's intent, ideas, judgments, tasks,
  hypotheses, constraints, and follow-ups without inventing a meeting structure.
- Generic content uses the structure best supported by the transcript and does not fill
  irrelevant sections merely to satisfy a template.
- Final synthesis always reads the complete corrected transcript. It does not depend only
  on chunk summaries, fixed item counts, or metadata-oriented filler.
- Important names, numbers, viewpoints, decisions, and tasks remain faithful to the
  evidence; uncertainty and conflicts remain explicit rather than being silently resolved.
- Each content-type profile requires representative human-reviewed samples and regression
  checks before it is considered to have met this baseline.

### 4.7 Progressive experience

After upload verification, the plugin displays:

- current stage;
- percentage and processed audio duration;
- elapsed time and estimated remaining time when reliable;
- completed transcript segments;
- a clearly marked active provisional segment;
- speaker-labeling status;
- warnings, pauses, and recovery actions.

The Worker persists each completed segment. Reconnecting clients reconstruct the latest preview from a snapshot and event cursor.

### 4.8 Output

The permanent output is a knowledge package rather than a single note. It contains:

- a human-facing main note;
- an evidence transcript with stable segment IDs;
- a machine-readable `speech-record.json`;
- the immutable raw ASR response;
- optional archived source audio when explicitly enabled.

Important generated statements link to stable transcript evidence.

### 4.9 Review and correction

- Raw ASR output is never overwritten.
- Users may correct text, terminology, speaker names, speaker attribution, and paragraph boundaries.
- Corrections are recorded separately from raw evidence.
- Summaries can be regenerated from the corrected transcript.
- Users may revise the optional recording context and rerun downstream cleanup and note
  synthesis without overwriting immutable raw ASR evidence.
- User-owned sections such as `我的补充` are protected from regeneration.
- A changed generated section creates a new revision rather than silently replacing user edits.

### 4.10 Job control

- Multiple queued jobs.
- One full local processing job active by default.
- Pause, resume, cancel, retry, and safe recovery.
- Completed chunks survive cancellation and failure.
- Job and publication state persist across Worker restarts.

## 5. Resource safeguards

### 5.1 Disk

Before upload acceptance or model download, the Worker estimates:

- source staging space;
- decoded and temporary audio space;
- model download space;
- generated artifacts;
- safety reserve.

V1 requires the larger of 20 GB or 10% of the target volume to remain free. Near the threshold, the UI warns. Below it, the operation is blocked. Speech Capture never deletes unrelated user files to recover space.

### 5.2 Memory and performance

The Worker monitors memory pressure, swap growth, free disk, heartbeat, and throughput.

- Moderate pressure produces a visible warning.
- Severe sustained pressure pauses at a safe chunk boundary.
- Completed chunks remain valid.
- The user chooses when to resume or switch to a lighter model profile.

## 6. Sync and transport independence

The Worker API is independent from the user's Vault synchronization provider.

- Local processing uses the same API over loopback.
- Remote processing uses a configurable HTTPS Worker endpoint.
- Tailscale is the recommended V1 private-network setup, not a protocol dependency.
- A synchronized-folder queue may exist as an optional fallback.
- Google Drive-specific authorization is not part of the core V1 plugin.

## 7. Completion semantics

Speech Capture exposes two independent states:

- **Processed:** the Worker has safely produced and stored the complete artifact package.
- **Published:** at least one authorized publisher has atomically written and verified the package in a Vault.

The UI must never collapse these into one ambiguous success state.

## 8. Cloud fallback

Cloud providers are optional and disabled by default.

- ASR fallback and summarization fallback are separately configured.
- A local failure does not cause automatic upload.
- Each cloud retry explains what data will leave the device and requires explicit consent.
- If transcription is already local, a summary fallback must not upload the source audio.
- Credentials stay outside the Vault.

V1 may define provider interfaces without shipping a cloud provider implementation.

## 9. Platform support

### V1

- Speech Worker: Apple Silicon macOS.
- Obsidian client: Obsidian Desktop is the supported target.
- Worker Manager: macOS.

### Later

- Windows and Linux Workers through alternative model backends.
- Mobile submission after a dedicated security, storage, and background-execution review.

## 10. Out of scope for V1

- Cross-recording voiceprint recognition.
- Interview-intent recognition.
- Live microphone transcription and streaming broadcast workflows.
- Automatic full translation.
- Automatic cloud fallback.
- Provider-specific Vault synchronization.
- Public internet exposure of a home Worker.
- Commercial distribution and paid hosted processing.

The schema may reserve optional fields for future interview-intent analysis without implementing the feature.

## 11. Frontend design and approval gate

The Obsidian user-facing pages are implemented only after:

- the Worker and protocol behaviors required by the personal V1 are stable enough to drive every visible state;
- page information architecture, end-to-end flows, state coverage, and protocol-to-interface mappings are documented;
- key interaction visuals are produced with the GPT Image capability provided through Codex, using synthetic data only;
- the project owner reviews the design and gives explicit approval.

Backend and protocol work continues before this approval. User-facing plugin page code does not. The required deliverables and approval checklist are defined in [Obsidian Frontend Design Gate](frontend-design-gate.md).

## 12. Acceptance principles

A V1 release is not acceptable unless:

- every decodable time range has an explicit outcome;
- no chunk is silently dropped;
- no completed job has a truncated ASR result;
- raw evidence is retained;
- all critical decisions and actions in the structured note are evidence-linked;
- unsupported critical claims are absent;
- restart, disconnect, low-disk, and memory-pressure paths have been tested;
- the approved frontend design states and recovery actions are represented in the implementation;
- private test audio and credentials remain outside Git.
