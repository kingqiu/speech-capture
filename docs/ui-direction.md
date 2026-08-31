# UI Direction

## 1. Experience goal

Speech Capture should feel like a calm native Obsidian tool for long-running work, not a model-control dashboard.

The interface must make five things immediately clear:

1. what will be processed;
2. where it will be processed;
3. what is happening now;
4. whether the transcript is provisional or durable;
5. what the user needs to do next.

Detailed visual design follows after the processing contract is implemented and tested, and before any user-facing Obsidian page code is written.

## 2. Design-before-code gate

The Obsidian frontend has an explicit approval gate:

1. stabilize the Worker and protocol states needed by the personal V1;
2. design the page architecture, flows, state matrix, and data mappings;
3. generate key interaction visuals through the GPT Image capability provided by Codex;
4. review and revise the designs with the project owner;
5. begin frontend implementation only after explicit approval.

The complete deliverables, visual set, safety rules, and approval criteria are defined in [Obsidian Frontend Design Gate](frontend-design-gate.md). Until that gate passes, backend and protocol work may continue, but the task workbench and transcript reader pages remain unimplemented.

## 3. Reference direction

The visual language should align with [Reading Capture](https://github.com/kingqiu/reading-capture-plugin), especially its restrained information density, state clarity, and Obsidian-native behavior.

The first visual baseline is:

- dark ink-green foundation;
- green for ready, healthy, and primary action;
- blue for active processing;
- amber for attention and recoverable waiting;
- red for failure or unsafe resource state;
- compact typography with generous line height for long text;
- clear hierarchy without decorative gradients or oversized cards.

Speech Capture may share principles and tokens with Reading Capture. It should not copy implementation code without an explicit license and dependency review.

## 4. Two main surfaces

### 4.1 Task workbench

The task workbench is optimized for submission, progress, warnings, and control.

It contains:

- source selector and source facts;
- target Worker and connection state;
- recording-date suggestion and confirmation;
- model profile and optional content-type override;
- upload and processing progress;
- progressive transcript preview;
- resource warning panel;
- pause, resume, cancel, retry, and open-result actions;
- separate `processed` and `published` status.

### 4.2 Transcript reader

The reader is optimized for evidence review.

It contains:

- synchronized transcript and audio position;
- stable segments;
- speaker labels and rename controls;
- correction state;
- uncertainty markers;
- evidence links from structured findings;
- regenerate-summary controls with a clear diff;
- protected human notes.

Task operations should not crowd the reading surface after publication.

## 5. Primary flow

```mermaid
flowchart LR
    Select["Select audio"] --> Review["Review source and date"]
    Review --> Upload["Upload and verify"]
    Upload --> Process["Watch progressive transcript"]
    Process --> Check["Review warnings and speakers"]
    Check --> Publish["Publish to Vault"]
    Publish --> Read["Read, correct, and regenerate"]
```

The default path should require few decisions. Advanced model and transport controls remain available without dominating the first screen.

## 6. Progressive transcript behavior

- Stable committed segments use normal transcript styling.
- The active tail is labeled `临时结果` and uses a subtle distinct treatment.
- Speaker attribution still in progress is shown as a status, not hidden.
- A later speaker-label update does not look like deleted text.
- Reconnect reconstructs the latest snapshot without duplicating segments.
- Users can scroll earlier content while the live tail continues.
- Following the live tail is an explicit toggle.

The preview is useful immediately but never presented as the final evidence artifact.

## 7. State language

Status labels describe both the system state and the next action:

| State | Meaning shown to the user | Primary action |
| --- | --- | --- |
| Ready | Worker and models are available | Start processing |
| Uploading | Source is transferring and resumable | Pause |
| Verifying | File integrity and decoding are being checked | Wait |
| Queued | Accepted and safe on the Worker | Reorder or pause |
| Processing | Durable progress is being produced | View transcript |
| Needs attention | User input or resources are required | Resolve issue |
| Safe paused | Work was paused without losing completed chunks | Resume or choose lighter profile |
| Processed | Artifacts are safely stored on the Worker | Publish |
| Published | Vault package was written and verified | Open note |
| Partial | Exact unresolved ranges remain | Review and retry |
| Failed | Processing stopped with a known reason | View recovery options |

Messages avoid vague labels such as `Something went wrong`.

## 8. Resource warnings

Warnings appear in context and remain available in task history.

Examples:

- Disk warning: show free space, estimated need, reserve rule, and why processing is blocked.
- Memory pressure: show whether work is merely slower or has safely paused.
- Model missing: show download size, installation state, and alternative profile.
- Remote Worker unavailable: show that accepted jobs remain on the Worker, if known.

The product never suggests that it cleaned user files. Cleanup actions target only explicitly listed Worker-owned data.

## 9. First-run Worker setup

The macOS Worker Manager guides the user through:

1. hardware and system check;
2. application-data location and disk reserve;
3. Worker service installation;
4. ASR profile download;
5. Ollama and summary-model readiness;
6. optional pyannote authorization;
7. private-network endpoint check;
8. device pairing;
9. a short local test recording.

Each step can report `ready`, `optional`, `needs action`, or `blocked`.

## 10. Settings organization

Plugin settings are grouped by user intent:

- Workers and pairing;
- default processing profile;
- output location and date behavior;
- language and terminology;
- source retention;
- optional cloud fallback;
- diagnostics and privacy;
- advanced compatibility.

Secrets are never displayed after entry. The UI shows presence, scope, and revoke or replace actions.

## 11. Accessibility and Obsidian fit

- Use Obsidian variables and components where practical.
- Support light and dark themes, even if the primary reference is dark.
- Do not rely on color alone.
- Preserve keyboard focus and sensible tab order.
- Announce meaningful progress changes without reading every incremental update.
- Respect reduced motion.
- Keep controls usable in narrow sidebars and detached windows.
- Use localized plain language; V1 starts with Simplified Chinese and English-ready message keys.

## 12. Design production sequence

After the backend contract is stable, the design phase produces high-fidelity interaction images, component specifications, empty and failure states, motion behavior, and final tokens. Visuals use synthetic content for privacy, while state sizing is validated with representative long-transcript fixtures.

The project owner reviews this complete design set before implementation. Approval is a prerequisite for Obsidian page development, not a final polish step after the pages already exist.
