import { ItemView, Modal, setIcon, type WorkspaceLeaf } from "obsidian";

import type {
  CorrectionSchema,
  JobSchema,
  JobSnapshotResponse,
  PublicationStatusResponse,
  SummaryRevisionListResponse,
  SummaryRevisionSchema,
  TranscriptSegmentSchema
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

import {
  nextConnectionAttempt,
  recoveryAfterFailure,
  type ConnectionAttemptMode,
  type ConnectionRecovery
} from "./connection-recovery";
import {
  estimateJobDiskBytes,
  formatBytes,
  formatDurationSeconds,
  isSupportedAudioFile,
  mediaTypeLabel,
  readAudioDurationSeconds,
  recordingDateHint,
  suggestRecordingDate,
  type RecordingDateSuggestion
} from "./intake-draft";
import { sameJobListPresentation } from "./job-list-refresh";
import type SpeechCapturePlugin from "./main";
import {
  applyJobAction,
  decideJobSummaryRevision,
  effectiveSpeakerDisplayName,
  effectiveTranscriptSegment,
  getJobSnapshot,
  JobClientError,
  listJobCorrections,
  listJobSummaryRevisions,
  listJobs,
  regenerateJobSummary,
  renameJobSpeakerDisplayName,
  reviewTranscriptSegment
} from "./job-client";
import { ObsidianWorkerTransport } from "./obsidian-worker-transport";
import {
  acknowledgePublication,
  claimPublication,
  downloadPublicationPackage,
  getPublicationStatus,
  PublicationClientError,
  releasePublication,
  type DownloadedPublicationPackage
} from "./publication-client";
import { loadReviewAudioSegment } from "./review-audio-client";
import {
  isSegmentReviewDraftDirty,
  segmentReviewDraftKey,
  type SegmentReviewDraft
} from "./review-draft";
import {
  SubmissionError,
  submitRecording,
  type SubmissionProgress
} from "./upload-client";
import {
  canCancelJob,
  jobProgressLabel,
  jobStageIndex,
  resourcePresentation,
  taskStatePresentation
} from "./task-state-presentation";
import {
  confirmPairingTicket,
  probeWorker,
  type WorkerProbeResult
} from "./worker-probe";
import { workbenchLayoutSize } from "./workbench-layout";
import { buildSummaryChanges, countSummaryChanges } from "./summary-diff";
import {
  chooseNewPublicationPath,
  inspectPublicationTarget,
  VaultPublicationError,
  writePublicationPackage,
  type PublicationConflictDiff
} from "./vault-publication";

export const WORKBENCH_VIEW_TYPE = "speech-capture-workbench";

const CONTENT_TYPE_OPTIONS = Object.freeze([
  ["auto", "内容类型 · 自动判断"],
  ["meeting", "会议"],
  ["interview", "访谈"],
  ["course", "课程 / 讲座"],
  ["speech", "演讲"],
  ["voice_memo", "个人语音笔记"],
  ["generic", "通用记录"]
] as const);

const JOB_STAGES = Object.freeze([
  "上传",
  "验证",
  "排队",
  "转写",
  "对齐",
  "说话人",
  "提炼",
  "质量检查"
] as const);

interface IntakeDraft {
  file: File | null;
  recordingDate: string;
  context: string;
  profile: "accuracy" | "speed";
  contentType: "auto" | "meeting" | "interview" | "course" | "speech" | "voice_memo" | "generic";
}

type SubmissionState =
  | { readonly state: "idle" }
  | { readonly state: "running"; readonly progress: SubmissionProgress }
  | { readonly state: "complete"; readonly jobId: string }
  | { readonly state: "error"; readonly message: string };

type PublicationViewState =
  | { readonly state: "idle" }
  | { readonly state: "loading" }
  | { readonly state: "publishing"; readonly targetRelativePath: string }
  | { readonly state: "waiting_other_client"; readonly targetRelativePath: string }
  | {
      readonly state: "conflict";
      readonly status: PublicationStatusResponse;
      readonly packageData: DownloadedPublicationPackage;
      readonly diff: PublicationConflictDiff;
      readonly viewed: boolean;
    }
  | { readonly state: "published"; readonly targetRelativePath: string }
  | { readonly state: "error"; readonly message: string };

export class SpeechWorkbenchView extends ItemView {
  private viewMode: "intake" | "pairing" | "task" = "intake";
  private taskDetailMode:
    | "review"
    | "summary"
    | "history"
    | "publication" = "review";
  private workerProbe: WorkerProbeResult | null = null;
  private probingWorker = false;
  private pairingTicket = "";
  private pairingState:
    | { readonly state: "idle" }
    | { readonly state: "submitting" }
    | { readonly state: "error"; readonly message: string } = { state: "idle" };
  private fileError: string | null = null;
  private recordingDateSource: RecordingDateSuggestion["source"] = "today";
  private recordingDateEdited = false;
  private sourceDurationSeconds: number | null = null;
  private submissionState: SubmissionState = { state: "idle" };
  private jobs: readonly JobSchema[] = [];
  private selectedJobId: string | null = null;
  private selectedSnapshot: JobSnapshotResponse | null = null;
  private corrections: readonly CorrectionSchema[] = [];
  private summaryRevisions: SummaryRevisionListResponse | null = null;
  private selectedSummaryRevisionKey: string | null = null;
  private summaryDecisionSaving = false;
  private summaryRegenerating = false;
  private publicationState: PublicationViewState = { state: "idle" };
  private publicationBusy = false;
  private selectedSegmentId: string | null = null;
  private speakerFilterId: string | null = null;
  private speakerSearch = "";
  private reviewSaving = false;
  private speakerRenameSaving = false;
  private reviewAudioUrl: string | null = null;
  private readonly localAudioByJobId = new Map<string, File>();
  private readonly segmentReviewDrafts = new Map<string, SegmentReviewDraft>();
  private readonly lastAnnouncedStableSegmentByJobId = new Map<string, string>();
  private taskError: string | null = null;
  private connectionRecovery: ConnectionRecovery | null = null;
  private refreshingTasks = false;
  private refreshTimer: number | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private workbenchEl: HTMLElement | null = null;
  private workbenchWidth = 0;
  private draft: IntakeDraft = {
    file: null,
    recordingDate: localDate(new Date()),
    context: "",
    profile: "accuracy",
    contentType: "auto"
  };

  public constructor(
    leaf: WorkspaceLeaf,
    private readonly plugin: SpeechCapturePlugin
  ) {
    super(leaf);
  }

  public override getViewType(): string {
    return WORKBENCH_VIEW_TYPE;
  }

  public override getDisplayText(): string {
    return "语音工作台";
  }

  public override getIcon(): string {
    return "audio-waveform";
  }

  public override async onOpen(): Promise<void> {
    this.resizeObserver = new ResizeObserver((entries) => {
      const entry = entries.at(-1);
      if (!entry) {
        return;
      }
      const previousSize = workbenchLayoutSize(
        this.workbenchWidth || this.contentEl.clientWidth
      );
      this.workbenchWidth = entry.contentRect.width;
      const nextSize = workbenchLayoutSize(this.workbenchWidth);
      if (this.workbenchEl && previousSize !== nextSize) {
        this.render();
      } else {
        this.applyWorkbenchLayoutSize();
      }
    });
    this.resizeObserver.observe(this.contentEl);
    this.render();
    await this.refreshWorker();
    this.refreshTimer = window.setInterval(() => {
      void this.pollJobs();
    }, 3_000);
  }

  public override async onClose(): Promise<void> {
    this.pairingTicket = "";
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.workbenchEl = null;
    if (this.refreshTimer !== null) {
      window.clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    this.releaseReviewAudioUrl();
  }

  public async onWorkerSettingsChanged(): Promise<void> {
    this.workerProbe = null;
    this.probingWorker = false;
    this.pairingTicket = "";
    this.pairingState = { state: "idle" };
    this.jobs = [];
    this.selectedJobId = null;
    this.selectedSnapshot = null;
    this.corrections = [];
    this.summaryRevisions = null;
    this.connectionRecovery = null;
    this.viewMode = "intake";
    this.render();
    await this.refreshWorker();
  }

  private render(): void {
    this.contentEl.empty();
    this.contentEl.addClass("speech-capture-view");
    const workbench = this.contentEl.createDiv({ cls: "speech-capture-workbench" });
    this.workbenchEl = workbench;
    this.applyWorkbenchLayoutSize();
    this.renderHeader(workbench);

    const layout = workbench.createDiv({ cls: "speech-capture-layout" });
    layout.toggleClass(
      "is-left-collapsed",
      this.plugin.settings.leftSidebarCollapsed
    );
    layout.toggleClass(
      "is-right-collapsed",
      this.plugin.settings.rightSidebarCollapsed
    );
    if (this.viewMode === "pairing") {
      layout.removeClass("is-left-collapsed", "is-right-collapsed");
      layout.addClass("is-pairing");
      this.renderDeviceSidebar(layout);
      this.renderPairing(layout);
      this.renderPairingConfirmation(layout);
    } else if (this.viewMode === "task" && this.selectedJobId) {
      this.renderTaskSidebar(layout);
      if (
        this.taskDetailMode === "publication" ||
        this.selectedSnapshot?.job.state === "publishing" ||
        this.selectedSnapshot?.job.state === "published"
      ) {
        this.renderPublication(layout);
        this.renderPublicationSidebar(layout);
      } else if (this.selectedSnapshot?.job.state === "processed") {
        if (this.taskDetailMode === "summary") {
          this.renderSummaryDiff(layout);
          this.renderSummaryDecisionSidebar(layout);
        } else if (this.taskDetailMode === "history") {
          this.renderSummaryHistory(layout);
          this.renderSummaryHistorySidebar(layout);
        } else {
          this.renderTranscriptReview(layout);
          this.renderReviewSidebar(layout);
        }
      } else {
        this.renderActiveTask(layout);
        this.renderCurrentTask(layout);
      }
      this.renderRestoreHandles(layout);
    } else {
      this.renderTaskSidebar(layout);
      this.renderIntake(layout);
      this.renderConfirmation(layout);
      this.renderRestoreHandles(layout);
    }
  }

  private applyWorkbenchLayoutSize(): void {
    if (!this.workbenchEl) {
      return;
    }
    const width = this.workbenchWidth || this.contentEl.clientWidth;
    const size = workbenchLayoutSize(width);
    this.workbenchEl.toggleClass("is-compact", size !== "wide");
    this.workbenchEl.toggleClass("is-narrow", size === "narrow");
  }

  private renderHeader(parent: HTMLElement): void {
    const header = parent.createEl("header", { cls: "speech-capture-header" });
    const identity = header.createDiv({ cls: "speech-capture-header__identity" });
    identity.createEl("h1", { text: "语音工作台" });
    identity.createEl("p", { text: "把长录音安全地转成逐字稿和可用笔记" });

    const preferredWorker = this.plugin.preferredWorker();
    const statusPresentation = this.workerStatusPresentation(preferredWorker?.displayName);
    const status = header.createDiv({
      cls: `speech-capture-status ${statusPresentation.className}`,
      attr: { role: "status" }
    });
    status.createSpan({ cls: "speech-capture-status__dot" });
    status.createSpan({
      text: statusPresentation.text
    });
  }

  private renderTaskSidebar(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-task-panel",
      attr: { "aria-label": "任务" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "任务" });
    title.appendChild(
      this.collapseButton("left", "收起任务栏", "panel-left-close")
    );

    const newTask = aside.createEl("button", {
      cls: `speech-capture-new-task ${this.viewMode === "intake" ? "is-selected" : ""}`,
      text: "新建任务",
      attr: {
        type: "button",
        ...(this.viewMode === "intake" ? { "aria-current": "page" } : {})
      }
    });
    const icon = createSpan({ cls: "speech-capture-new-task__icon" });
    setIcon(icon, "plus");
    newTask.prepend(icon);
    newTask.addEventListener("click", () => this.openIntake());
    if (this.jobs.length > 0) {
      for (const job of this.jobs) {
        const task = aside.createEl("button", {
          cls: `speech-capture-task-card ${job.job_id === this.selectedJobId ? "is-selected" : ""}`,
          attr: {
            type: "button",
            ...(job.job_id === this.selectedJobId
              ? { "aria-current": "page" }
              : {})
          }
        });
        const row = task.createSpan({ cls: "speech-capture-task-card__title" });
        const wave = row.createSpan({ cls: "speech-capture-task-card__icon" });
        setIcon(wave, "audio-lines");
        row.createEl("strong", { text: taskTitle(job.source_display_name) });
        task.createEl("span", {
          text: this.taskCardStatus(job)
        });
        if (job.recording_date) {
          task.createEl("small", { text: job.recording_date });
        }
        task.addEventListener("click", () => void this.selectJob(job.job_id));
      }
    } else {
      aside.createEl("p", {
        cls: "speech-capture-empty-copy",
        text: "提交第一段录音后，任务会显示在这里。"
      });
    }
  }

  private renderActiveTask(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-active-task"
    });
    const snapshot = this.selectedSnapshot;
    const job = snapshot?.job ?? this.selectedJob();
    if (!job) {
      main.createEl("p", { text: "正在读取任务…" });
      return;
    }
    const heading = main.createDiv({ cls: "speech-capture-active-task__heading" });
    const copy = heading.createDiv();
    copy.createEl("p", { cls: "speech-capture-eyebrow", text: "ACTIVE TASK" });
    copy.createEl("h2", { text: taskTitle(job.source_display_name) });
    heading.createEl("span", {
      cls: `speech-capture-job-state is-${jobStateTone(job.state)}`,
      text: jobStateLabel(job.state)
    });

    this.renderStageRail(main, snapshot, job);
    this.renderResourceNotice(main, snapshot);
    this.renderTaskStateNotice(main, snapshot);
    this.renderTaskProgress(main, snapshot);
    this.renderTranscriptPreview(main, snapshot);
    if (this.connectionRecovery) {
      const error = main.createDiv({ cls: "speech-capture-inline-warning" });
      if (this.connectionRecovery.state === "retrying") {
        error.createSpan({
          text: `连接中断，系统将在 1 分钟后自动重试（已尝试 ${this.connectionRecovery.attemptsCompleted}/3 次）`
        });
      } else {
        error.createSpan({ text: "三次自动重试仍未恢复，请手动重试。" });
        const retry = error.createEl("button", {
          text: `重新连接${this.plugin.preferredWorker()?.displayName ?? " Worker"}`,
          attr: { type: "button" }
        });
        retry.addEventListener("click", () => void this.refreshJobs("manual"));
      }
    } else if (this.taskError) {
      main.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: this.taskError,
        attr: { role: "alert" }
      });
    }
  }

  private renderTranscriptReview(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-review"
    });
    const snapshot = this.selectedSnapshot;
    if (!snapshot) {
      main.createEl("p", { text: "正在读取完整逐字稿…" });
      return;
    }
    const selected = this.selectedReviewSegment();
    const heading = main.createDiv({ cls: "speech-capture-review__heading" });
    const copy = heading.createDiv();
    copy.createEl("h2", { text: "逐字稿与证据复核" });
    copy.createEl("p", {
      text: "点击文字定位音频；修订只影响校订稿，不覆盖原始识别证据"
    });
    const headingActions = heading.createDiv({
      cls: "speech-capture-review__heading-actions"
    });
    const pendingRevision = this.pendingSummaryRevision();
    if (pendingRevision) {
      const compare = headingActions.createEl("button", {
        text: `查看候选笔记 v${pendingRevision.candidate_version.toString()}`,
        attr: { type: "button" }
      });
      compare.addEventListener("click", () => {
        this.selectedSummaryRevisionKey = pendingRevision.revision_key;
        this.taskDetailMode = "summary";
        this.render();
      });
    } else if (this.summaryRevisions?.can_regenerate) {
      const regenerate = headingActions.createEl("button", {
        cls: "mod-cta",
        text: this.summaryRegenerating ? "正在重新生成…" : "重新生成笔记",
        attr: { type: "button" }
      });
      regenerate.disabled = this.summaryRegenerating;
      regenerate.addEventListener("click", () => void this.regenerateSummary());
    }
    if ((this.summaryRevisions?.revisions.length ?? 0) > 0) {
      const history = headingActions.createEl("button", {
        text: "版本记录",
        attr: { type: "button" }
      });
      history.addEventListener("click", () => {
        this.taskDetailMode = "history";
        this.render();
      });
    }
    headingActions.createEl("span", {
      cls: "speech-capture-job-state is-good",
      text: "处理完成 · 可复核"
    });

    this.renderReviewAudio(main, snapshot, selected);
    if (this.taskError) {
      main.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: this.taskError,
        attr: { role: "alert" }
      });
    }

    const section = main.createDiv({ cls: "speech-capture-review-transcript" });
    section.createEl("h3", { text: "完整校订逐字稿" });
    const speakerIds = this.reviewSpeakerIds();
    const filters = section.createDiv({ cls: "speech-capture-review-filters" });
    const allSpeakers = filters.createEl("button", {
      cls: this.speakerFilterId === null ? "is-selected" : "",
      text: "全部",
      attr: {
        type: "button",
        "aria-pressed": this.speakerFilterId === null ? "true" : "false"
      }
    });
    allSpeakers.addEventListener("click", () => this.selectSpeakerFilter(null));
    for (const speakerId of speakerIds) {
      const filter = filters.createEl("button", {
        cls: this.speakerFilterId === speakerId ? "is-selected" : "",
        text: this.speakerPrimaryLabel(speakerId),
        attr: {
          type: "button",
          "aria-pressed": this.speakerFilterId === speakerId ? "true" : "false"
        }
      });
      filter.addEventListener("click", () => this.selectSpeakerFilter(speakerId));
    }
    const revisedCount = snapshot.stable_segments.filter(
      (segment) => effectiveTranscriptSegment(segment, this.corrections).revised
    ).length;
    if (revisedCount > 0) {
      filters.createEl("span", {
        cls: "speech-capture-review-filters__count",
        text: `已修订 ${revisedCount.toString()}`
      });
    }

    const rows = section.createDiv({ cls: "speech-capture-review-rows" });
    const narrow = workbenchLayoutSize(
      this.workbenchWidth || this.contentEl.clientWidth
    ) === "narrow";
    for (const segment of snapshot.stable_segments) {
      if (segment.outcome !== "transcribed") {
        continue;
      }
      const effective = effectiveTranscriptSegment(segment, this.corrections);
      if (
        this.speakerFilterId !== null &&
        effective.speakerId !== this.speakerFilterId
      ) {
        continue;
      }
      const row = rows.createEl("button", {
        cls: `speech-capture-review-row ${segment.segment_id === selected?.segment_id ? "is-selected" : ""}`,
        attr: {
          type: "button",
          "aria-pressed": segment.segment_id === selected?.segment_id ? "true" : "false",
          "data-segment-id": segment.segment_id
        }
      });
      row.createEl("time", { text: formatDuration(segment.start_ms) });
      row.createEl("strong", {
        text: effective.speakerId
          ? this.speakerPrimaryLabel(effective.speakerId)
          : speakerLabel(null)
      });
      row.createEl("span", {
        cls: "speech-capture-review-row__text",
        text: effective.text
      });
      row.createEl("small", { text: segment.segment_id });
      if (effective.revised) {
        row.createEl("span", {
          cls: "speech-capture-review-row__revised",
          text: "已修订"
        });
      }
      row.addEventListener("click", () => {
        this.selectReviewSegment(segment, narrow);
      });
      if (narrow && segment.segment_id === selected?.segment_id) {
        this.renderSegmentEditor(rows, segment, true);
      }
    }
  }

  private renderReviewAudio(
    parent: HTMLElement,
    snapshot: JobSnapshotResponse,
    selected: TranscriptSegmentSchema | null
  ): void {
    const card = parent.createDiv({ cls: "speech-capture-review-audio" });
    const audio = card.createEl("audio", { attr: { preload: "none" } });
    const play = card.createEl("button", {
      cls: "speech-capture-review-play",
      attr: {
        type: "button",
        "aria-label": selected ? "播放当前片段" : "没有可播放片段"
      }
    });
    const playIcon = play.createSpan();
    setIcon(playIcon, "play");
    const duration = Math.max(
      snapshot.progress?.duration_ms ?? 0,
      ...snapshot.stable_segments.map((segment) => segment.end_ms)
    );
    const current = selected?.start_ms ?? 0;
    const time = card.createEl("strong", {
      text: `${formatDuration(current)} / ${formatDuration(duration)}`
    });
    const slider = card.createEl("input", {
      cls: "speech-capture-review-audio__slider",
      attr: {
        type: "range",
        min: "0",
        max: Math.max(1, duration).toString(),
        step: "1000",
        value: current.toString(),
        "aria-label": "音频时间位置"
      }
    });
    slider.addEventListener("input", () => {
      time.setText(
        `${formatDuration(Number(slider.value))} / ${formatDuration(duration)}`
      );
    });
    slider.addEventListener("change", () => {
      const target = Number(slider.value);
      const nearest = snapshot.stable_segments.find(
        (segment) => segment.start_ms <= target && segment.end_ms >= target
      ) ?? snapshot.stable_segments.reduce<TranscriptSegmentSchema | null>(
        (best, segment) =>
          best === null ||
          Math.abs(segment.start_ms - target) < Math.abs(best.start_ms - target)
            ? segment
            : best,
        null
      );
      if (nearest) {
        this.selectedSegmentId = nearest.segment_id;
        this.render();
      }
    });
    const workerName = this.plugin.preferredWorker()?.displayName ?? "Worker";
    const offline = this.connectionRecovery !== null;
    const source = card.createDiv({ cls: "speech-capture-review-audio__source" });
    const sourceCopy = source.createDiv({
      cls: "speech-capture-review-audio__source-copy"
    });
    sourceCopy.createSpan({
      text: offline
        ? "当前无法播放音频，逐字稿仍可阅读和修改"
        : this.localAudioByJobId.has(snapshot.job.job_id)
          ? "当前设备原始音频 · 本地播放"
          : `${workerName} 在线 · 流式播放`
    });
    if (offline) {
      play.disabled = true;
      slider.disabled = true;
      if (this.connectionRecovery?.state === "retrying") {
        sourceCopy.createEl("small", {
          text: `系统将在 1 分钟后自动重试（已尝试 ${this.connectionRecovery.attemptsCompleted}/3 次）`
        });
      } else {
        const reconnect = source.createEl("button", {
          text: `重新连接${workerName}`,
          attr: { type: "button" }
        });
        reconnect.addEventListener("click", () =>
          void this.refreshJobs("manual")
        );
      }
    } else {
      play.disabled = selected === null;
      play.addEventListener("click", () => {
        if (selected) {
          void this.playReviewSegment(audio, snapshot.job.job_id, selected, play);
        }
      });
    }
    const navigation = card.createDiv({ cls: "speech-capture-review-audio__nav" });
    const segments = snapshot.stable_segments.filter(
      (segment) => segment.outcome === "transcribed"
    );
    const index = selected
      ? segments.findIndex((segment) => segment.segment_id === selected.segment_id)
      : -1;
    const previous = navigation.createEl("button", {
      text: "上一条证据",
      attr: { type: "button" }
    });
    previous.disabled = index <= 0;
    previous.addEventListener("click", () =>
      this.selectReviewSegment(segments[index - 1], this.isNarrowWorkbench())
    );
    const next = navigation.createEl("button", {
      text: "下一条证据",
      attr: { type: "button" }
    });
    next.disabled = index < 0 || index >= segments.length - 1;
    next.addEventListener("click", () =>
      this.selectReviewSegment(segments[index + 1], this.isNarrowWorkbench())
    );
  }

  private renderReviewSidebar(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-review-sidebar",
      attr: { "aria-label": "当前片段" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "当前片段" });
    title.appendChild(
      this.collapseButton("right", "收起当前片段栏", "panel-right-close")
    );
    const selected = this.selectedReviewSegment();
    if (!selected) {
      aside.createEl("p", {
        cls: "speech-capture-empty-copy",
        text: "选择一段逐字稿后，可以在这里复核。"
      });
      return;
    }
    aside.createEl("p", {
      cls: "speech-capture-review-sidebar__range",
      text: `${formatDuration(selected.start_ms)}–${formatDuration(selected.end_ms)} · 已对齐 · 原始证据已保存`
    });
    this.renderSpeakerDisplayNameEditor(aside, selected);
    const narrow = workbenchLayoutSize(
      this.workbenchWidth || this.contentEl.clientWidth
    ) === "narrow";
    if (!narrow) {
      this.renderSegmentEditor(aside, selected, false);
    }
    const evidence = aside.createDiv({ cls: "speech-capture-review-evidence" });
    evidence.createEl("h3", { text: "证据状态" });
    this.assurance(evidence, "circle-check", "原始识别已保存");
    this.assurance(evidence, "circle-check", "时间范围已对齐");
    evidence.createEl("p", {
      text: "保存修订会新增一条记录，不会重写原始 ASR。"
    });
  }

  private renderSummaryDiff(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-summary-diff"
    });
    const revision = this.selectedSummaryRevision();
    if (!revision) {
      main.createEl("p", {
        cls: "speech-capture-empty-copy",
        text: "当前没有可比较的笔记候选版本。"
      });
      return;
    }
    const heading = main.createDiv({ cls: "speech-capture-summary-heading" });
    const copy = heading.createDiv();
    copy.createEl("p", { cls: "speech-capture-eyebrow", text: "NOTE REVISION" });
    copy.createEl("h2", { text: "比较重新生成的笔记" });
    copy.createEl("p", {
      text: "逐字稿修订已用于生成候选笔记；确认前不会替换当前 Note。"
    });
    const protectedBadge = heading.createEl("span", {
      cls: "speech-capture-summary-protected",
      text: "原始证据已保护"
    });
    protectedBadge.prepend(this.choiceMark("shield-check"));

    const versions = main.createDiv({ cls: "speech-capture-summary-versions" });
    const base = versions.createDiv();
    base.createEl("span", { text: "当前版本" });
    base.createEl("strong", { text: `v${revision.base_version.toString()}` });
    versions.createSpan({ cls: "speech-capture-summary-versions__arrow", text: "→" });
    const candidate = versions.createDiv({ cls: "is-candidate" });
    candidate.createEl("span", { text: "候选版本" });
    candidate.createEl("strong", { text: `v${revision.candidate_version.toString()}` });
    candidate.createEl("small", {
      text: summaryRevisionStatusLabel(revision.status)
    });

    const changes = buildSummaryChanges(
      revision.before_document,
      revision.after_document
    );
    const counts = countSummaryChanges(changes);
    const overview = main.createDiv({ cls: "speech-capture-summary-overview" });
    overview.createEl("h3", { text: "本次机器提炼变化" });
    overview.createEl("p", {
      text: `新增 ${counts.added.toString()} 处 · 修改 ${counts.modified.toString()} 处 · 移除 ${counts.removed.toString()} 处`
    });
    if (revision.text_correction_count || revision.speaker_rename_count) {
      overview.createEl("small", {
        text: `依据 ${revision.text_correction_count.toString()} 处文字修订、${revision.speaker_rename_count.toString()} 个说话人改名重新生成`
      });
    }

    const list = main.createDiv({ cls: "speech-capture-summary-change-list" });
    if (changes.length === 0) {
      list.createEl("p", {
        cls: "speech-capture-empty-copy",
        text: "机器提炼内容没有产生可见变化。"
      });
    }
    for (const change of changes) {
      const card = list.createDiv({
        cls: `speech-capture-summary-change is-${change.kind}`
      });
      const title = card.createDiv({ cls: "speech-capture-summary-change__title" });
      title.createEl("h3", { text: change.label });
      title.createEl("span", { text: summaryChangeKindLabel(change.kind) });
      const columns = card.createDiv({ cls: "speech-capture-summary-change__columns" });
      this.renderSummaryVersionText(columns, "修改前", change.beforeText, "before");
      this.renderSummaryVersionText(columns, "修改后", change.afterText, "after");
      if (change.evidenceIds.length > 0) {
        const evidence = card.createEl("p", {
          cls: "speech-capture-summary-change__evidence",
          text: `关联原始证据 ${change.evidenceIds.length.toString()} 段`
        });
        evidence.prepend(this.choiceMark("link-2"));
      }
    }

    const manual = main.createDiv({ cls: "speech-capture-summary-manual" });
    const manualTitle = manual.createDiv();
    manualTitle.createEl("h3", { text: "我的补充" });
    manualTitle.createEl("span", { text: "受保护 · 不参与版本切换" });
    manual.createEl("p", {
      text: manualSectionBody(
        this.summaryRevisions?.manual_section_markdown ?? ""
      ) || "当前没有人工补充内容。"
    });

    const history = main.createEl("button", {
      cls: "speech-capture-summary-history-link",
      text: "查看版本记录",
      attr: { type: "button" }
    });
    history.prepend(this.choiceMark("history"));
    history.addEventListener("click", () => {
      this.taskDetailMode = "history";
      this.render();
    });

    if (this.isNarrowWorkbench()) {
      this.renderSummaryDecisionPanel(main, revision, true);
    }
  }

  private renderSummaryVersionText(
    parent: HTMLElement,
    label: string,
    text: string,
    tone: "before" | "after"
  ): void {
    const column = parent.createDiv({
      cls: `speech-capture-summary-version-text is-${tone}`
    });
    column.createEl("span", { text: label });
    column.createEl("p", { text: text || "（无）" });
  }

  private renderSummaryDecisionSidebar(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-summary-sidebar",
      attr: { "aria-label": "版本确认" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "版本确认" });
    title.appendChild(
      this.collapseButton("right", "收起版本确认栏", "panel-right-close")
    );
    const revision = this.selectedSummaryRevision();
    if (revision) {
      this.renderSummaryDecisionPanel(aside, revision, false);
    }
  }

  private renderSummaryDecisionPanel(
    parent: HTMLElement,
    revision: SummaryRevisionSchema,
    inline: boolean
  ): void {
    const panel = parent.createDiv({
      cls: `speech-capture-summary-decision ${inline ? "is-inline" : ""}`
    });
    panel.createEl("h3", { text: "确认这份候选笔记" });
    this.assurance(panel, "sparkles", "只切换机器提炼的 Note 正文");
    this.assurance(panel, "shield-check", "原始 ASR 与证据不会变化");
    this.assurance(panel, "notebook-pen", "“我的补充”保持当前内容");
    if (this.taskError) {
      panel.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: this.taskError
      });
    }
    if (revision.status !== "pending") {
      const status = panel.createDiv({
        cls: `speech-capture-summary-decision__status is-${revision.status}`
      });
      setIcon(status.createSpan(), revision.status === "accepted" ? "circle-check" : "circle-x");
      status.createEl("strong", {
        text:
          revision.status === "accepted"
            ? `v${revision.candidate_version.toString()} 已成为当前笔记`
            : `已保留 v${revision.base_version.toString()} 作为当前笔记`
      });
      status.createEl("p", { text: "此记录只读，原始证据仍然保留。" });
    } else {
      const accept = panel.createEl("button", {
        cls: "mod-cta speech-capture-summary-decision__accept",
        text: this.summaryDecisionSaving ? "正在保存…" : "接受新版笔记",
        attr: { type: "button" }
      });
      accept.disabled = this.summaryDecisionSaving;
      accept.addEventListener("click", () =>
        void this.saveSummaryDecision(revision, "accepted")
      );
      const continueReview = panel.createEl("button", {
        text: "继续修改逐字稿",
        attr: { type: "button" }
      });
      continueReview.disabled = this.summaryDecisionSaving;
      continueReview.addEventListener("click", () => {
        this.taskDetailMode = "review";
        this.render();
      });
      const reject = panel.createEl("button", {
        cls: "speech-capture-summary-decision__reject",
        text: "不采用新版",
        attr: { type: "button" }
      });
      reject.disabled = this.summaryDecisionSaving;
      reject.addEventListener("click", () =>
        void this.saveSummaryDecision(revision, "rejected")
      );
      panel.createEl("p", {
        cls: "speech-capture-summary-decision__hint",
        text: "接受新版笔记：新版将成为当前 Note，旧版和本次差异仍可查看。"
      });
      panel.createEl("p", {
        cls: "speech-capture-summary-decision__hint",
        text: "不采用新版：当前 Note 保持不变，候选版会以未采用状态保留。"
      });
    }
  }

  private renderSummaryHistory(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-summary-history"
    });
    const heading = main.createDiv({ cls: "speech-capture-summary-heading" });
    const copy = heading.createDiv();
    copy.createEl("p", { cls: "speech-capture-eyebrow", text: "READ-ONLY HISTORY" });
    copy.createEl("h2", { text: "版本记录" });
    copy.createEl("p", { text: "这里仅用于查看生成与确认记录，不提供回滚、删除或逐项合并。" });
    const back = heading.createEl("button", {
      text: "返回逐字稿复核",
      attr: { type: "button" }
    });
    back.addEventListener("click", () => {
      this.taskDetailMode = "review";
      this.render();
    });

    const current = main.createDiv({ cls: "speech-capture-summary-current-version" });
    current.createEl("span", { text: "当前使用" });
    current.createEl("strong", {
      text: `v${(this.summaryRevisions?.current_version ?? 1).toString()}`
    });
    const list = main.createDiv({ cls: "speech-capture-summary-history-list" });
    const revisions = [...(this.summaryRevisions?.revisions ?? [])].reverse();
    if (revisions.length === 0) {
      list.createEl("p", {
        cls: "speech-capture-empty-copy",
        text: "当前还没有重新生成记录。"
      });
    }
    for (const revision of revisions) {
      const row = list.createDiv({ cls: "speech-capture-summary-history-row" });
      const versions = row.createDiv();
      versions.createEl("strong", {
        text: `v${revision.base_version.toString()} → v${revision.candidate_version.toString()}`
      });
      versions.createEl("small", {
        text: `${formatSummaryTimestamp(revision.created_at)} · ${summaryRevisionStatusLabel(revision.status)}`
      });
      const inspect = row.createEl("button", {
        text: "查看差异",
        attr: { type: "button" }
      });
      inspect.addEventListener("click", () => {
        this.selectedSummaryRevisionKey = revision.revision_key;
        this.taskDetailMode = "summary";
        this.render();
      });
    }
    if (this.isNarrowWorkbench()) {
      this.renderSummaryHistoryExplanation(main, true);
    }
  }

  private renderSummaryHistorySidebar(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-summary-sidebar",
      attr: { "aria-label": "版本记录说明" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "记录说明" });
    title.appendChild(
      this.collapseButton("right", "收起记录说明栏", "panel-right-close")
    );
    this.renderSummaryHistoryExplanation(aside, false);
  }

  private renderSummaryHistoryExplanation(
    parent: HTMLElement,
    inline: boolean
  ): void {
    const panel = parent.createDiv({
      cls: `speech-capture-summary-history-help ${inline ? "is-inline" : ""}`
    });
    panel.createEl("h3", { text: "第一版保持简单" });
    this.assurance(panel, "eye", "可查看每次候选的前后差异");
    this.assurance(panel, "lock-keyhole", "已确认记录保持只读");
    this.assurance(panel, "shield-check", "原始 ASR 和人工补充不随版本切换");
    panel.createEl("p", { text: "回滚、删除和复杂版本管理留到后续版本。" });
  }

  private renderPublication(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-publication"
    });
    const job = this.selectedSnapshot?.job ?? this.selectedJob();
    if (!job) {
      main.createEl("p", { text: "正在读取发布状态…" });
      return;
    }
    const heading = main.createDiv({ cls: "speech-capture-publication__heading" });
    const copy = heading.createDiv();
    copy.createEl("p", { cls: "speech-capture-eyebrow", text: "ACTIVE TASK" });
    copy.createEl("h2", { text: taskTitle(job.source_display_name) });
    heading.createEl("span", {
      cls: "speech-capture-job-state is-good",
      text: job.state === "published" ? "已发布" : "已处理"
    });
    this.renderPublicationRail(main);

    const state = this.publicationState;
    if (state.state === "conflict") {
      if (state.viewed) {
        this.renderPublicationConflictDiff(main, state);
      } else {
        this.renderPublicationConflictNotice(main, state);
      }
      return;
    }
    if (state.state === "published") {
      const card = main.createDiv({ cls: "speech-capture-publication-result is-success" });
      card.setAttrs({ role: "status", "aria-live": "polite" });
      setIcon(card.createSpan(), "circle-check-big");
      const result = card.createDiv();
      result.createEl("h3", { text: "已发布到 Obsidian" });
      result.createEl("p", { text: "完整产物包已经写入当前 Vault，并通过写入后校验。" });
      const open = result.createEl("button", {
        cls: "mod-cta",
        text: "打开 Note",
        attr: { type: "button" }
      });
      open.addEventListener("click", () => void this.openPublishedNote(state.targetRelativePath));
      return;
    }
    if (state.state === "waiting_other_client") {
      const card = main.createDiv({ cls: "speech-capture-publication-result is-waiting" });
      card.setAttrs({ role: "status", "aria-live": "polite" });
      setIcon(card.createSpan(), "clock-3");
      const result = card.createDiv();
      result.createEl("h3", { text: "另一台已授权设备正在发布" });
      result.createEl("p", { text: "完成后会自动同步状态；当前 Worker 产物保持不变。" });
      return;
    }
    if (state.state === "error") {
      const card = main.createDiv({ cls: "speech-capture-publication-result is-error" });
      card.setAttrs({ role: "alert", "aria-live": "assertive" });
      setIcon(card.createSpan(), "shield-alert");
      const result = card.createDiv();
      result.createEl("h3", { text: "发布尚未完成" });
      result.createEl("p", { text: state.message });
      const retry = result.createEl("button", {
        cls: "mod-cta",
        text: "重新检测并发布",
        attr: { type: "button" }
      });
      retry.addEventListener("click", () => void this.preparePublication(true));
      return;
    }

    const card = main.createDiv({ cls: "speech-capture-publication-result is-waiting" });
    card.setAttrs({ role: "status", "aria-live": "polite" });
    setIcon(card.createSpan(), state.state === "publishing" ? "refresh-cw" : "circle-check-big");
    const result = card.createDiv();
    result.createEl("h3", {
      text:
        state.state === "publishing"
          ? "正在写入 Obsidian"
          : "处理完成，等待自动发布"
    });
    result.createEl("p", {
      text:
        state.state === "publishing"
          ? "正在把完整产物写入同级临时目录，校验通过后一次性完成发布。"
          : "完整产物已安全保存在 Worker；已授权客户端连接后会自动发布。"
    });
    const artifacts = main.createDiv({ cls: "speech-capture-publication-artifacts" });
    artifacts.createEl("h3", { text: "已生成的产物（已校验通过）" });
    for (const label of ["纯净 Note", "时间线", "完整校订逐字稿", "证据与记录"]) {
      this.assurance(artifacts, "circle-check", label);
    }
    artifacts.createEl("p", {
      cls: "speech-capture-field__hint",
      text: "发布前，这些文件不会出现在当前 Vault。"
    });
  }

  private renderPublicationConflictNotice(
    main: HTMLElement,
    state: Extract<PublicationViewState, { state: "conflict" }>
  ): void {
    const card = main.createDiv({ cls: "speech-capture-publication-conflict" });
    setIcon(card.createSpan({ cls: "speech-capture-publication-conflict__icon" }), "triangle-alert");
    const copy = card.createDiv();
    copy.createEl("h3", { text: "目标位置已有修改" });
    copy.createEl("p", { text: "检测到当前 Vault 的目标目录在任务处理后发生过变化。" });
    this.assurance(copy, "shield-check", "当前 Vault 内容和 Worker 待发布版本都没有被覆盖");
    const comparison = copy.createDiv({ cls: "speech-capture-publication-compare-cards" });
    const current = comparison.createDiv();
    current.createEl("strong", { text: "当前 Vault 内容" });
    current.createEl("p", {
      text: state.diff.changedFiles.length
        ? `发现 ${state.diff.changedFiles.length.toString()} 个文件存在人工或同步变化`
        : "目录结构或文件集合已经发生变化"
    });
    const worker = comparison.createDiv();
    worker.createEl("strong", { text: "Worker 待发布版本" });
    worker.createEl("p", { text: "完整产物已校验，尚未写入当前 Vault" });
    const view = copy.createEl("button", {
      cls: "mod-cta speech-capture-publication-primary",
      text: "查看差异",
      attr: { type: "button" }
    });
    view.addEventListener("click", () => {
      this.publicationState = { ...state, viewed: true };
      this.render();
    });
    copy.createEl("p", {
      cls: "speech-capture-publication-hint",
      text: "查看差异后，可以选择保存到新位置，不会覆盖当前内容。"
    });
  }

  private renderPublicationConflictDiff(
    main: HTMLElement,
    state: Extract<PublicationViewState, { state: "conflict" }>
  ): void {
    const heading = main.createDiv({ cls: "speech-capture-publication-diff-heading" });
    heading.createEl("h3", { text: "已查看发布差异" });
    heading.createEl("p", { text: "当前 Vault 的修改与 Worker 待发布版本都已保留。" });
    const diff = main.createDiv({ cls: "speech-capture-publication-diff" });
    const current = diff.createDiv();
    current.createEl("strong", { text: "当前 Vault 版本" });
    this.renderPublicationHighlights(
      current,
      state.diff.currentNoteHighlights,
      "当前 Note 与待发布版本不同；原位置保持不变。"
    );
    const worker = diff.createDiv({ cls: "is-worker" });
    worker.createEl("strong", { text: "Worker 待发布版本" });
    this.renderPublicationHighlights(
      worker,
      state.diff.workerNoteHighlights,
      "Worker 的完整产物包已通过校验。"
    );
    const safeguards = main.createDiv({ cls: "speech-capture-publication-safeguards" });
    this.assurance(safeguards, "shield-check", "当前位置不变：保留现有人工与同步修改");
    this.assurance(safeguards, "shield-check", "Worker 版本不变：不做覆盖或逐条合并");
    this.assurance(safeguards, "shield-check", "新位置写入后再次校验完整性");
    const save = main.createEl("button", {
      cls: "mod-cta speech-capture-publication-primary",
      text: this.publicationBusy ? "正在保存…" : "保存到新位置",
      attr: { type: "button" }
    });
    save.disabled = this.publicationBusy;
    save.addEventListener("click", () => void this.savePublicationToNewLocation(state));
    main.createEl("p", {
      cls: "speech-capture-publication-hint",
      text: "将创建一个新的任务目录并重新校验，不会覆盖当前内容。"
    });
    const back = main.createEl("button", {
      cls: "speech-capture-publication-back",
      text: "返回冲突说明",
      attr: { type: "button" }
    });
    back.addEventListener("click", () => {
      this.publicationState = { ...state, viewed: false };
      this.render();
    });
  }

  private renderPublicationHighlights(
    parent: HTMLElement,
    highlights: readonly string[],
    fallback: string
  ): void {
    if (!highlights.length) {
      parent.createEl("p", { text: fallback });
      return;
    }
    const list = parent.createEl("ul");
    for (const line of highlights) {
      list.createEl("li", { text: line });
    }
  }

  private renderPublicationSidebar(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-publication-sidebar",
      attr: { "aria-label": "发布目标" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", {
      text: this.publicationState.state === "conflict" && this.publicationState.viewed
        ? "解决方式"
        : this.publicationState.state === "conflict"
          ? "冲突位置"
          : "发布目标"
    });
    title.appendChild(
      this.collapseButton("right", "收起发布目标栏", "panel-right-close")
    );
    const target = publicationTargetPath(this.publicationState);
    const facts = aside.createDiv({ cls: "speech-capture-publication-sidebar__facts" });
    this.assurance(facts, "vault", "当前 Obsidian Vault");
    this.assurance(facts, "folder", target ?? this.plugin.settings.outputFolder);
    if (this.publicationState.state === "conflict") {
      this.assurance(facts, "clock-3", "发现内容变化");
    } else if (this.publicationState.state === "published") {
      this.assurance(facts, "circle-check", "写入并校验成功");
    } else {
      this.assurance(facts, "refresh-cw", "发布方式 · 自动");
    }
    const safety = aside.createDiv({ cls: "speech-capture-publication-sidebar__safety" });
    safety.createEl("h3", { text: "安全检查" });
    this.assurance(safety, "circle-check", "产物校验通过");
    this.assurance(safety, "circle-check", "原始 ASR 已保留");
    this.assurance(safety, "circle-check", "“我的补充”不会被原位置覆盖");
  }

  private renderPublicationRail(parent: HTMLElement): void {
    const labels = [...JOB_STAGES, "发布"];
    const published = this.publicationState.state === "published";
    const rail = parent.createDiv({ cls: "speech-capture-stage-rail" });
    for (const [index, label] of labels.entries()) {
      const final = index === labels.length - 1;
      const item = rail.createDiv({
        cls: `speech-capture-stage ${!final || published ? "is-complete" : "is-current"}`
      });
      const mark = item.createSpan({ cls: "speech-capture-stage__mark" });
      if (!final || published) {
        setIcon(mark, "check");
      }
      item.createSpan({ text: label });
    }
  }

  private renderSegmentEditor(
    parent: HTMLElement,
    segment: TranscriptSegmentSchema,
    inline: boolean
  ): void {
    const effective = effectiveTranscriptSegment(segment, this.corrections);
    const draftKey = this.segmentReviewDraftKey(segment.segment_id);
    const draft = this.segmentReviewDrafts.get(draftKey) ?? {
      text: effective.text,
      speakerId: effective.speakerId
    };
    const editor = parent.createDiv({
      cls: `speech-capture-segment-editor ${inline ? "is-inline" : ""}`
    });
    const speaker = editor.createDiv({ cls: "speech-capture-segment-editor__group" });
    speaker.createEl("h3", {
      text: "这段话是谁说的？",
      attr: inline ? { tabindex: "-1" } : {}
    });
    speaker.createEl("p", { text: "只修正当前这一段，不会影响其他段落。" });
    const search = speaker.createEl("input", {
      attr: {
        type: "search",
        placeholder: "搜索说话人",
        value: this.speakerSearch,
        "aria-label": "搜索说话人"
      }
    });
    const select = speaker.createEl("select", {
      attr: { size: "6", "aria-label": "选择当前片段说话人" }
    });
    const uncertain = select.createEl("option", {
      text: "暂不确定",
      value: ""
    });
    uncertain.selected = draft.speakerId === null;
    for (const speakerId of this.reviewSpeakerIds()) {
      const option = select.createEl("option", {
        text: this.speakerOptionLabel(speakerId),
        value: speakerId
      });
      option.selected = speakerId === draft.speakerId;
    }
    const applySpeakerSearch = (): void => {
      const needle = search.value.trim().toLocaleLowerCase();
      for (const option of Array.from(select.options)) {
        option.hidden =
          option.value !== "" && !option.text.toLocaleLowerCase().includes(needle);
      }
    };
    search.addEventListener("input", () => {
      this.speakerSearch = search.value;
      applySpeakerSearch();
    });
    applySpeakerSearch();

    const text = editor.createDiv({ cls: "speech-capture-segment-editor__group" });
    text.createEl("h3", { text: "文字校订" });
    const textarea = text.createEl("textarea", {
      attr: {
        rows: "5",
        "aria-label": "当前片段校订文字"
      }
    });
    textarea.value = draft.text;
    text.createEl("p", {
      text: "原始 ASR 不会被改写，修改会记录为新修订。"
    });
    const save = editor.createEl("button", {
      cls: "mod-cta speech-capture-segment-editor__save",
      text: this.reviewSaving ? "正在保存…" : "保存此段修订",
      attr: { type: "button" }
    });
    const updateDisabled = (): void => {
      const nextDraft = {
        text: textarea.value,
        speakerId: select.value || null
      };
      if (!isSegmentReviewDraftDirty(nextDraft, effective)) {
        this.segmentReviewDrafts.delete(draftKey);
      } else {
        this.segmentReviewDrafts.set(draftKey, nextDraft);
      }
      save.disabled =
        this.reviewSaving ||
        !textarea.value.trim() ||
        (textarea.value.trim() === effective.text &&
          (select.value || null) === effective.speakerId);
    };
    textarea.addEventListener("input", updateDisabled);
    select.addEventListener("change", updateDisabled);
    updateDisabled();
    save.addEventListener("click", () =>
      void this.saveSegmentReview(
        segment,
        effective.text,
        effective.speakerId,
        textarea.value.trim(),
        select.value || null
      )
    );
  }

  private renderSpeakerDisplayNameEditor(
    parent: HTMLElement,
    selectedSegment: TranscriptSegmentSchema
  ): void {
    const speakerIds = this.reviewSpeakerIds();
    if (speakerIds.length === 0) {
      return;
    }
    const selectedEffective = effectiveTranscriptSegment(
      selectedSegment,
      this.corrections
    );
    const initialSpeakerId =
      selectedEffective.speakerId && speakerIds.includes(selectedEffective.speakerId)
        ? selectedEffective.speakerId
        : speakerIds[0]!;
    const editor = parent.createDiv({
      cls: "speech-capture-speaker-name-editor"
    });
    editor.createEl("h3", { text: "说话人显示名" });
    editor.createEl("p", {
      text: "批量改名只改变人物标签，不会改变任何片段的说话人归属。"
    });
    const select = editor.createEl("select", {
      attr: { "aria-label": "选择要批量改名的说话人" }
    });
    for (const speakerId of speakerIds) {
      const option = select.createEl("option", {
        text: this.speakerOptionLabel(speakerId),
        value: speakerId
      });
      option.selected = speakerId === initialSpeakerId;
    }
    const input = editor.createEl("input", {
      attr: {
        type: "text",
        maxlength: "200",
        placeholder: "例如：访谈嘉宾",
        "aria-label": "新的说话人显示名"
      }
    });
    const save = editor.createEl("button", {
      text: this.speakerRenameSaving ? "正在保存…" : "批量改显示名",
      attr: { type: "button" }
    });
    const loadSelectedName = (): void => {
      const current = effectiveSpeakerDisplayName(select.value, this.corrections);
      input.value = current.revised ? current.displayName : "";
    };
    const updateDisabled = (): void => {
      const current = effectiveSpeakerDisplayName(select.value, this.corrections);
      save.disabled =
        this.speakerRenameSaving ||
        !input.value.trim() ||
        input.value.trim() === current.displayName;
    };
    select.addEventListener("change", () => {
      loadSelectedName();
      updateDisabled();
    });
    input.addEventListener("input", updateDisabled);
    loadSelectedName();
    updateDisabled();
    save.addEventListener("click", () => {
      const current = effectiveSpeakerDisplayName(select.value, this.corrections);
      void this.saveSpeakerDisplayName(
        select.value,
        current.displayName,
        input.value.trim()
      );
    });
  }

  private renderCurrentTask(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-current-task",
      attr: { "aria-label": "当前任务" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "当前任务" });
    title.appendChild(
      this.collapseButton("right", "收起当前任务栏", "panel-right-close")
    );
    const job = this.selectedSnapshot?.job ?? this.selectedJob();
    const facts = aside.createDiv({ cls: "speech-capture-task-facts" });
    this.assurance(
      facts,
      "monitor",
      `Worker · ${this.plugin.preferredWorker()?.displayName ?? "-"}`
    );
    this.assurance(
      facts,
      "wifi",
      this.connectionRecovery ? "连接待恢复" : "连接稳定"
    );
    this.assurance(facts, "circle-check", "上传已完成");
    this.assurance(
      facts,
      "calendar-days",
      `录音日期 · ${job?.recording_date ?? "待确认"}`
    );
    const resource = resourcePresentation(this.selectedSnapshot?.resource_report ?? null);
    if (resource) {
      this.assurance(facts, resource.icon, resource.shortText);
    }

    const actions = aside.createDiv({ cls: "speech-capture-current-actions" });
    actions.createEl("h3", { text: "当前可做" });
    const statePresentation = job
      ? taskStatePresentation(job, this.selectedSnapshot?.resource_report ?? null)
      : null;
    const blockedResource = resourcePresentation(
      this.selectedSnapshot?.resource_report ?? null
    );
    if (
      blockedResource?.kind === "blocked" &&
      blockedResource.actionLabel &&
      (job?.state === "waiting_user" || job?.state === "failed")
    ) {
      const retryResource = actions.createEl("button", {
        text: blockedResource.actionLabel,
        attr: { type: "button" }
      });
      retryResource.prepend(this.choiceMark("refresh-cw"));
      retryResource.addEventListener("click", () =>
        void this.performTaskAction("retry")
      );
    } else if (statePresentation?.action && statePresentation.actionLabel) {
      const stateAction = statePresentation.action;
      const action = actions.createEl("button", {
        text: statePresentation.actionLabel,
        attr: { type: "button" }
      });
      action.prepend(
        this.choiceMark(
          stateAction === "resume"
            ? "play-circle"
            : stateAction === "new_task"
              ? "file-plus-2"
              : "rotate-cw"
        )
      );
      action.addEventListener("click", () => {
        if (stateAction === "new_task") {
          this.openIntake();
          return;
        }
        void this.performTaskAction(stateAction);
      });
    } else if (job && isPausable(job.state)) {
      const pause = actions.createEl("button", {
        text: "安全暂停",
        attr: { type: "button" }
      });
      pause.prepend(this.choiceMark("pause-circle"));
      pause.addEventListener("click", () => void this.performTaskAction("pause"));
    }
    if (job && isPausable(job.state)) {
      const background = actions.createEl("button", {
        text: "在后台继续",
        attr: { type: "button" }
      });
      background.prepend(this.choiceMark("play-circle"));
      background.addEventListener("click", () => this.openIntake());
    }
    if (job && canCancelJob(job.state)) {
      const cancel = actions.createEl("button", {
        cls: "speech-capture-task-cancel",
        text: "取消任务",
        attr: { type: "button" }
      });
      cancel.prepend(this.choiceMark("ban"));
      cancel.addEventListener("click", () => this.openCancelConfirmation());
    }
    actions.createEl("p", {
      text:
        job && isPausable(job.state)
          ? "上传已完成，现在可以关闭 Obsidian，Worker 会继续处理。"
          : "已上传音频、稳定逐字稿和处理检查点均会保留。"
    });
  }

  private renderIntake(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-intake"
    });
    main.createEl("p", {
      cls: "speech-capture-eyebrow",
      text: "NEW SPEECH TASK"
    });
    main.createEl("h2", { text: "新建语音任务" });

    this.renderSourceField(main);
    this.renderDateField(main);
    this.renderContextField(main);
    this.renderWorkerField(main);
    this.renderProfileField(main);

    const typeField = this.field(main, "内容类型");
    const select = typeField.createEl("select", {
      attr: { "aria-label": "内容类型" }
    });
    for (const [value, label] of CONTENT_TYPE_OPTIONS) {
      const option = select.createEl("option", { text: label, value });
      option.selected = this.draft.contentType === value;
    }
    select.disabled = this.submissionState.state === "running";
    select.addEventListener("change", () => {
      this.draft.contentType = select.value as IntakeDraft["contentType"];
    });
  }

  private renderSourceField(parent: HTMLElement): void {
    const field = this.field(parent, "来源文件");
    const dropZone = field.createDiv({
      cls: "speech-capture-drop-zone"
    });
    const input = dropZone.createEl("input", {
      cls: "speech-capture-visually-hidden",
      attr: {
        type: "file",
        accept: "audio/*",
        tabindex: "-1",
        "aria-label": "选择音频文件"
      }
    });
    input.disabled = this.submissionState.state === "running";
    const fileIcon = dropZone.createSpan({ cls: "speech-capture-file-icon" });
    setIcon(fileIcon, "file-audio");
    const details = dropZone.createDiv({ cls: "speech-capture-file-copy" });
    details.createEl("strong", {
      text: this.draft.file?.name ?? "拖放音频到这里"
    });
    details.createEl("span", {
      text: this.draft.file
        ? `${mediaTypeLabel(this.draft.file)} · ${formatDurationSeconds(this.sourceDurationSeconds)} · ${formatBytes(this.draft.file.size)}`
        : "或从这台电脑选择音频文件"
    });
    const choose = dropZone.createEl("button", {
      text: this.draft.file ? "更换文件" : "选择文件",
      attr: { type: "button" }
    });
    choose.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      void this.setFile(input.files?.[0] ?? null);
    });
    dropZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      dropZone.addClass("is-dragging");
    });
    dropZone.addEventListener("dragleave", () => {
      dropZone.removeClass("is-dragging");
    });
    dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropZone.removeClass("is-dragging");
      void this.setFile(event.dataTransfer?.files[0] ?? null);
    });
    if (this.fileError) {
      field.createEl("p", {
        cls: "speech-capture-field__error",
        text: this.fileError,
        attr: { role: "alert" }
      });
    }
  }

  private renderDateField(parent: HTMLElement): void {
    const field = this.field(parent, "录音日期");
    const input = field.createEl("input", {
      attr: {
        type: "date",
        value: this.draft.recordingDate,
        "aria-label": "录音日期"
      }
    });
    input.disabled = this.submissionState.state === "running";
    input.addEventListener("change", () => {
      this.draft.recordingDate = input.value;
      this.recordingDateEdited = true;
    });
    field.createEl("p", {
      cls: "speech-capture-field__hint",
      text: recordingDateHint(this.recordingDateSource)
    });
  }

  private renderContextField(parent: HTMLElement): void {
    const field = this.field(parent, "补充背景（可选）");
    const textarea = field.createEl("textarea", {
      attr: {
        placeholder: "可以写会议主题、参与者、公司名或你认为有帮助的任何信息",
        rows: "4",
        "aria-label": "补充背景（可选）"
      }
    });
    textarea.value = this.draft.context;
    textarea.disabled = this.submissionState.state === "running";
    textarea.addEventListener("input", () => {
      this.draft.context = textarea.value;
    });
    field.createEl("p", {
      cls: "speech-capture-field__hint",
      text: "只作为逐字稿校订和笔记提炼的参考，不会替代录音证据"
    });
  }

  private renderWorkerField(parent: HTMLElement): void {
    const field = this.field(parent, "处理位置");
    const choices = field.createDiv({ cls: "speech-capture-choice-grid" });
    const preferredWorker = this.plugin.preferredWorker();
    if (preferredWorker) {
      const worker = choices.createEl("button", {
        cls: `speech-capture-choice is-selected ${this.workerProbe?.state === "blocked" ? "is-blocked" : ""}`,
        text: this.workerStatusPresentation(preferredWorker.displayName).text,
        attr: { type: "button", "aria-pressed": "true" }
      });
      worker.prepend(this.choiceMark("server"));
      if (this.workerProbe?.state === "pairing_required") {
        worker.addEventListener("click", () => this.openPairing());
      }
    }
    const localCandidate = this.plugin.settings.workers.find(
      (worker) => worker.kind === "local"
    );
    if (localCandidate && localCandidate.id !== preferredWorker?.id) {
      const local = choices.createEl("button", {
        cls: "speech-capture-choice",
        text: "这台 Mac · 未检测到可用 Worker",
        attr: { type: "button", disabled: "true" }
      });
      local.prepend(this.choiceMark("laptop"));
    }
    if (!preferredWorker) {
      field.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: "尚未配置可用 Worker。请先连接家中 Worker，或重新检测这台 Mac。"
      });
    } else if (this.workerProbe?.state === "unreachable") {
      const warning = field.createDiv({ cls: "speech-capture-inline-warning" });
      warning.createSpan({
        text: `未检测到可用 Worker：${this.workerProbe.diagnostic}。任务和草稿不会切换到其他设备。`
      });
      const retry = warning.createEl("button", {
        text: "重新检测",
        attr: { type: "button" }
      });
      retry.addEventListener("click", () => void this.refreshWorker());
      const remote = preferredWorker.kind === "remote";
      const help = warning.createEl("button", {
        text: remote ? "管理处理设备" : "查看安装或启动说明",
        attr: { type: "button" }
      });
      help.addEventListener("click", () =>
        remote
          ? this.plugin.openWorkerSettings()
          : this.openLocalWorkerHelp()
      );
    } else if (this.workerProbe?.state === "pairing_required") {
      const warning = field.createDiv({ cls: "speech-capture-inline-warning" });
      warning.createSpan({ text: "需要连接此设备。配对完成前不会上传音频。" });
      const connect = warning.createEl("button", {
        text: "开始连接",
        attr: { type: "button" }
      });
      connect.addEventListener("click", () => this.openPairing());
    } else if (this.workerProbe?.state === "incompatible") {
      const warning = field.createDiv({ cls: "speech-capture-inline-warning" });
      warning.createSpan({
        text: "当前版本无法与此 Worker 一起使用。更新插件或 Worker 后请重新检测。"
      });
      const retry = warning.createEl("button", {
        text: "重新检测",
        attr: { type: "button" }
      });
      retry.addEventListener("click", () => void this.refreshWorker());
    } else if (this.workerProbe?.state === "warning") {
      const warning = field.createDiv({ cls: "speech-capture-inline-warning" });
      warning.createSpan({
        text: this.workerProbe.readiness.issue_codes.includes(
          "SPEAKER_DIARIZATION_MODEL_MISSING"
        )
          ? "Worker 可以转写，但多人说话人识别模型尚未准备好；人物归属可能需要人工复核。"
          : "Worker 可以开始处理，但当前资源状态需要留意。"
      });
      const retry = warning.createEl("button", {
        text: "重新检测",
        attr: { type: "button" }
      });
      retry.addEventListener("click", () => void this.refreshWorker());
      const help = warning.createEl("button", {
        text: "查看处理说明",
        attr: { type: "button" }
      });
      help.addEventListener("click", () => this.openLocalWorkerHelp());
    } else if (this.workerProbe?.state === "blocked") {
      const warning = field.createDiv({ cls: "speech-capture-inline-warning" });
      warning.createSpan({
        text: "Worker 已连接，但资源或模型尚未准备好。"
      });
      const retry = warning.createEl("button", {
        text: "重新检测",
        attr: { type: "button" }
      });
      retry.addEventListener("click", () => void this.refreshWorker());
      const help = warning.createEl("button", {
        text: "查看处理说明",
        attr: { type: "button" }
      });
      help.addEventListener("click", () => this.openLocalWorkerHelp());
    }
  }

  private renderProfileField(parent: HTMLElement): void {
    const field = this.field(parent, "处理模式");
    const choices = field.createDiv({ cls: "speech-capture-choice-grid" });
    const accuracy = this.profileButton(
      choices,
      "accuracy",
      "准确优先",
      "适合会议、访谈和重要记录",
      "scan"
    );
    const speed = this.profileButton(
      choices,
      "speed",
      "速度优先",
      "资源占用更低",
      "gauge"
    );
    accuracy.addEventListener("click", () => this.setProfile("accuracy"));
    speed.addEventListener("click", () => this.setProfile("speed"));
    accuracy.disabled = this.submissionState.state === "running";
    speed.disabled = this.submissionState.state === "running";
  }

  private renderConfirmation(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-confirmation",
      attr: { "aria-label": "提交前确认" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.appendChild(
      this.collapseButton("right", "收起提交确认栏", "panel-right-close")
    );
    title.createEl("h2", { text: "提交前确认" });

    const assurances = aside.createDiv({ cls: "speech-capture-assurances" });
    this.assurance(assurances, "shield-check", "原始音频不会被修改");
    this.assurance(assurances, "notebook-pen", "补充背景只作为参考");
    this.assurance(assurances, "cloud-upload", "上传完成后可关闭 Obsidian");

    const facts = aside.createDiv({ cls: "speech-capture-facts" });
    facts.createEl("h3", { text: "来源文件" });
    this.fact(facts, "文件名", this.draft.file?.name ?? "尚未选择");
    this.fact(
      facts,
      "文件大小",
      this.draft.file ? formatBytes(this.draft.file.size) : "-"
    );
    this.fact(
      facts,
      "时长",
      this.draft.file ? formatDurationSeconds(this.sourceDurationSeconds) : "-"
    );
    this.fact(facts, "录音日期", this.draft.file ? this.draft.recordingDate : "-");
    const estimate = this.draft.file
      ? estimateJobDiskBytes(this.draft.file.size, this.sourceDurationSeconds)
      : null;
    facts.createEl("h3", { text: "Worker 预计占用" });
    this.fact(
      facts,
      "上传验证峰值",
      estimate ? `约 ${formatBytes(estimate.uploadPeakBytes)}` : "验证音频后确认"
    );
    this.fact(
      facts,
      "处理临时文件",
      estimate ? `约 ${formatBytes(estimate.workingBytes)}` : "验证音频后确认"
    );
    this.fact(
      facts,
      "预计峰值总占用",
      estimate ? `约 ${formatBytes(estimate.totalBytes)}` : "验证音频后确认"
    );

    if (this.submissionState.state !== "idle") {
      this.renderSubmissionStatus(aside);
    }

    const actions = aside.createDiv({ cls: "speech-capture-actions" });
    const cancel = actions.createEl("button", {
      text: this.submissionState.state === "complete" ? "新建任务" : "取消",
      attr: { type: "button" }
    });
    cancel.disabled = this.submissionState.state === "running";
    cancel.addEventListener("click", () => this.resetDraft());
    const submit = actions.createEl("button", {
      cls: "mod-cta",
      text: submissionButtonLabel(this.submissionState),
      attr: { type: "button" }
    });
    submit.disabled =
      this.submissionState.state === "running" ||
      this.submissionState.state === "complete" ||
      !(this.draft.file && this.selectedProfileCanStart());
    submit.addEventListener("click", () => void this.submitDraft());
  }

  private renderDeviceSidebar(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-device-panel",
      attr: { "aria-label": "处理设备" }
    });
    const title = aside.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "处理设备" });
    const retry = title.createEl("button", {
      cls: "speech-capture-text-button",
      text: "重新检测",
      attr: { type: "button" }
    });
    retry.prepend(this.choiceMark("refresh-cw"));
    retry.addEventListener("click", () => void this.refreshWorker());

    const worker = this.plugin.preferredWorker();
    const card = aside.createEl("button", {
      cls: "speech-capture-device-card is-selected",
      attr: { type: "button", "aria-current": "true" }
    });
    card.appendChild(this.choiceMark(worker?.kind === "local" ? "laptop" : "monitor"));
    const copy = card.createSpan();
    copy.createEl("strong", { text: worker?.displayName ?? "Worker" });
    copy.createEl("small", { text: "已发现 · 尚未配对" });
    for (const candidate of this.plugin.settings.workers) {
      if (candidate.id === worker?.id) {
        continue;
      }
      const other = aside.createEl("button", {
        cls: "speech-capture-device-card",
        attr: { type: "button", disabled: "true" }
      });
      other.appendChild(
        this.choiceMark(candidate.kind === "local" ? "laptop" : "monitor")
      );
      const otherCopy = other.createSpan();
      otherCopy.createEl("strong", { text: candidate.displayName });
      otherCopy.createEl("small", {
        text:
          candidate.kind === "local"
            ? "未检测到可用 Worker"
            : "当前未选择"
      });
    }
  }

  private renderPairing(layout: HTMLElement): void {
    const main = layout.createEl("main", {
      cls: "speech-capture-panel speech-capture-pairing"
    });
    const worker = this.plugin.preferredWorker();
    const workerName = worker?.displayName ?? "Worker";
    main.createEl("p", { cls: "speech-capture-eyebrow", text: "FIRST-TIME SETUP" });
    main.createEl("h2", { text: `连接${workerName}` });
    main.createEl("p", {
      cls: "speech-capture-pairing__intro",
      text: `Worker 将在你的${workerName}上本地处理音频，数据只留在你的设备和 Vault 中。请完成一次配对与授权，以建立安全连接。`
    });

    const device = this.pairingSection(main, "monitor-check", "确认设备");
    device.createEl("p", { text: workerName });
    device.createEl("p", {
      text: worker?.kind === "local" ? "本机连接已发现" : "私有网络已连接"
    });
    device.createEl("p", { text: "版本兼容" });

    const code = this.pairingSection(main, "key-round", "输入配对码");
    code.createEl("p", {
      cls: "speech-capture-pairing__hint",
      text: `在${workerName}的 Worker Manager 中生成配对码。`
    });
    const input = code.createEl("input", {
      attr: {
        type: "text",
        value: this.pairingTicket,
        autocomplete: "one-time-code",
        autocapitalize: "off",
        spellcheck: "false",
        placeholder: "粘贴短时配对码",
        "aria-label": "配对码"
      }
    });
    const scope = this.pairingSection(main, "shield-check", "授权范围");
    scope.createEl("p", { text: `仅允许当前 Vault：${this.app.vault.getName()}` });
    scope.createEl("p", { text: "凭据安全保存到系统 Secret Storage" });

    if (this.pairingState.state === "error") {
      main.createEl("p", {
        cls: "speech-capture-pairing__error",
        text: this.pairingState.message,
        attr: { role: "alert" }
      });
    }
    const connect = main.createEl("button", {
      cls: "mod-cta speech-capture-pairing__submit",
      text: this.pairingState.state === "submitting" ? "正在连接" : "连接并授权",
      attr: { type: "button" }
    });
    connect.disabled =
      this.pairingState.state === "submitting" || !this.pairingTicket.trim();
    input.addEventListener("input", () => {
      this.pairingTicket = input.value;
      if (this.pairingState.state === "error") {
        this.pairingState = { state: "idle" };
        main.querySelector(".speech-capture-pairing__error")?.remove();
      }
      connect.disabled = !this.pairingTicket.trim();
    });
    connect.addEventListener("click", () => void this.submitPairing());
    const cancel = main.createEl("button", {
      cls: "speech-capture-text-button speech-capture-pairing__cancel",
      text: "暂不连接",
      attr: { type: "button" }
    });
    cancel.addEventListener("click", () => this.closePairing());
  }

  private renderPairingConfirmation(layout: HTMLElement): void {
    const aside = layout.createEl("aside", {
      cls: "speech-capture-panel speech-capture-pairing-confirmation",
      attr: { "aria-label": "连接前确认" }
    });
    aside.createEl("h2", { text: "连接前确认" });
    this.pairingConfirmationCard(
      aside,
      "cloud-off",
      "不会上传到公共云端",
      "音频与转录只在已连接的私有设备和 Vault 中处理。"
    );
    this.pairingConfirmationCard(
      aside,
      "vault",
      "只授权当前 Vault",
      "Worker 不能访问其他 Vault。"
    );
    this.pairingConfirmationCard(
      aside,
      "clock-3",
      "配对码仅在短时间内有效",
      "过期后需要在目标 Mac 上重新生成。"
    );
    aside.createEl("p", {
      cls: "speech-capture-inline-warning",
      text: `配对码需要在${this.plugin.preferredWorker()?.displayName ?? "Worker"}的 Worker Manager 中生成`
    });
    const help = aside.createDiv({ cls: "speech-capture-pairing-help" });
    help.createEl("h3", { text: "找不到配对码？" });
    help.createEl("p", {
      text: "请在目标 Mac 上打开 Worker Manager，生成新的配对码。"
    });
  }

  private pairingSection(
    parent: HTMLElement,
    iconName: string,
    title: string
  ): HTMLDivElement {
    const section = parent.createDiv({ cls: "speech-capture-pairing-section" });
    const icon = section.createSpan({ cls: "speech-capture-pairing-section__icon" });
    setIcon(icon, iconName);
    const body = section.createDiv({ cls: "speech-capture-pairing-section__body" });
    body.createEl("h3", { text: title });
    return body;
  }

  private pairingConfirmationCard(
    parent: HTMLElement,
    iconName: string,
    title: string,
    detail: string
  ): void {
    const card = parent.createDiv({ cls: "speech-capture-pairing-confirmation-card" });
    const icon = card.createSpan();
    setIcon(icon, iconName);
    const copy = card.createDiv();
    copy.createEl("strong", { text: title });
    copy.createEl("p", { text: detail });
  }

  private renderRestoreHandles(layout: HTMLElement): void {
    const left = layout.createEl("button", {
      cls: "speech-capture-restore-handle is-left",
      attr: { type: "button", "aria-label": "展开任务栏" }
    });
    setIcon(left, "chevron-right");
    left.addEventListener("click", () => void this.toggleSidebar("left", false));
    const hasUnsavedReview =
      this.taskDetailMode === "review" && this.hasUnsavedSelectedReviewDraft();
    const rightLabel =
      this.taskDetailMode === "publication" ||
      this.selectedSnapshot?.job.state === "publishing" ||
      this.selectedSnapshot?.job.state === "published"
        ? "展开发布目标栏"
        : this.selectedSnapshot?.job.state === "processed"
        ? this.taskDetailMode === "review"
          ? "展开当前片段栏"
          : this.taskDetailMode === "summary"
            ? "展开版本确认栏"
            : "展开记录说明栏"
        : this.viewMode === "task"
          ? "展开当前任务栏"
          : "展开提交确认栏";
    const right = layout.createEl("button", {
      cls: `speech-capture-restore-handle is-right ${hasUnsavedReview ? "has-unsaved-review" : ""}`,
      attr: {
        type: "button",
        "aria-label": hasUnsavedReview
          ? `${rightLabel}。当前片段有未保存的修订`
          : rightLabel
      }
    });
    setIcon(right, "chevron-left");
    right.addEventListener("click", () => void this.toggleSidebar("right", false));
  }

  private field(parent: HTMLElement, label: string): HTMLDivElement {
    const field = parent.createDiv({ cls: "speech-capture-field" });
    field.createEl("label", { text: label });
    return field;
  }

  private collapseButton(
    side: "left" | "right",
    label: string,
    iconName: string
  ): HTMLButtonElement {
    const button = createEl("button", {
      cls: "speech-capture-collapse-button",
      attr: { type: "button", "aria-label": label }
    });
    setIcon(button, iconName);
    button.addEventListener("click", () => void this.toggleSidebar(side, true));
    return button;
  }

  private choiceMark(iconName: string): HTMLSpanElement {
    const icon = createSpan({ cls: "speech-capture-choice__icon" });
    setIcon(icon, iconName);
    return icon;
  }

  private profileButton(
    parent: HTMLElement,
    profile: "accuracy" | "speed",
    title: string,
    hint: string,
    iconName: string
  ): HTMLButtonElement {
    const button = parent.createEl("button", {
      cls: `speech-capture-profile ${this.draft.profile === profile ? "is-selected" : ""}`,
      attr: {
        type: "button",
        "aria-pressed": this.draft.profile === profile ? "true" : "false"
      }
    });
    button.appendChild(this.choiceMark(iconName));
    const copy = button.createSpan();
    copy.createEl("strong", { text: title });
    copy.createEl("small", { text: hint });
    return button;
  }

  private renderStageRail(
    parent: HTMLElement,
    snapshot: JobSnapshotResponse | null,
    job: JobSchema
  ): void {
    const currentIndex = jobStageIndex(job.state, snapshot?.progress?.stage ?? null);
    const rail = parent.createDiv({ cls: "speech-capture-stage-rail" });
    for (const [index, label] of JOB_STAGES.entries()) {
      const item = rail.createDiv({
        cls: `speech-capture-stage ${index < currentIndex ? "is-complete" : index === currentIndex ? "is-current" : ""}`
      });
      const mark = item.createSpan({ cls: "speech-capture-stage__mark" });
      if (index < currentIndex) {
        setIcon(mark, "check");
      }
      item.createSpan({ text: label });
    }
  }

  private renderTaskProgress(
    parent: HTMLElement,
    snapshot: JobSnapshotResponse | null
  ): void {
    const card = parent.createDiv({ cls: "speech-capture-processing-card" });
    const duration = snapshot?.progress?.duration_ms ?? 0;
    const processed = snapshot?.progress?.processed_ms ?? 0;
    const progress = Math.round((snapshot?.progress?.stage_progress ?? 0) * 100);
    card.createEl("strong", {
      cls: "speech-capture-processing-time",
      text: `${formatDuration(processed)} / ${formatDuration(duration)}`
    });
    card.createEl("p", {
      text: snapshot?.progress
        ? jobProgressLabel(
            snapshot.job.state,
            progress,
            snapshot.progress.estimated_remaining_seconds
          )
        : snapshot?.job.state === "cancelled"
          ? "任务已停止，最后保存的内容仍可查看"
          : "任务已进入队列，等待 Worker 更新进度"
    });
    const track = card.createDiv({
      cls: "speech-capture-progress is-large",
      attr: {
        role: "progressbar",
        "aria-label": "当前处理阶段进度",
        "aria-valuemin": "0",
        "aria-valuemax": "100",
        "aria-valuenow": progress.toString(),
        "aria-valuetext": `${progress.toString()}%`
      }
    });
    track.createDiv({
      cls: "speech-capture-progress__fill",
      attr: { style: `width: ${progress}%` }
    });
    const safe = card.createDiv({ cls: "speech-capture-processing-safe" });
    const icon = safe.createSpan();
    setIcon(icon, "shield-check");
    safe.createSpan({
      text: `已完成的 ${snapshot?.stable_segments.length ?? 0} 个稳定片段均已安全保存`
    });
  }

  private renderResourceNotice(
    parent: HTMLElement,
    snapshot: JobSnapshotResponse | null
  ): void {
    const presentation = resourcePresentation(snapshot?.resource_report ?? null);
    if (!presentation || presentation.kind === "ready") {
      return;
    }
    const notice = parent.createDiv({
      cls: `speech-capture-resource-notice is-${presentation.kind}`
    });
    const icon = notice.createSpan({ cls: "speech-capture-resource-notice__icon" });
    setIcon(icon, presentation.icon);
    const copy = notice.createDiv();
    copy.createEl("strong", { text: presentation.title });
    copy.createEl("p", { text: presentation.detail });
    if (presentation.diskFacts) {
      const facts = copy.createDiv({ cls: "speech-capture-resource-notice__facts" });
      facts.createSpan({
        text: `预计还需要 ${formatBytes(presentation.diskFacts.requiredBytes)}`
      });
      facts.createSpan({
        text: `当前可用 ${formatBytes(presentation.diskFacts.availableBytes)}`
      });
      facts.createSpan({
        text: `安全保留 ${formatBytes(presentation.diskFacts.reserveBytes)}`
      });
    }
  }

  private renderTaskStateNotice(
    parent: HTMLElement,
    snapshot: JobSnapshotResponse | null
  ): void {
    if (!snapshot) {
      return;
    }
    const presentation = taskStatePresentation(
      snapshot.job,
      snapshot.resource_report ?? null
    );
    if (!presentation) {
      return;
    }
    const notice = parent.createDiv({
      cls: `speech-capture-resource-notice is-${presentation.kind}`
    });
    const icon = notice.createSpan({ cls: "speech-capture-resource-notice__icon" });
    setIcon(icon, presentation.icon);
    const copy = notice.createDiv();
    copy.createEl("strong", { text: presentation.title });
    copy.createEl("p", { text: presentation.detail });
  }

  private renderTranscriptPreview(
    parent: HTMLElement,
    snapshot: JobSnapshotResponse | null
  ): void {
    const panel = parent.createDiv({ cls: "speech-capture-transcript-preview" });
    panel.createEl("h3", { text: "逐字稿预览" });
    const meta = panel.createDiv({ cls: "speech-capture-transcript-preview__meta" });
    meta.createSpan({
      text: `稳定文字 ${snapshot?.stable_segments.length ?? 0} 段`
    });
    meta.createSpan({ text: "说话人识别将在转写后开始" });
    const rows = panel.createDiv({ cls: "speech-capture-transcript-preview__rows" });
    const stable = snapshot?.stable_segments.slice(-4) ?? [];
    for (const segment of stable) {
      const row = rows.createDiv({ cls: "speech-capture-transcript-row" });
      row.createEl("strong", { text: speakerLabel(segment.speaker_id) });
      row.createEl("time", { text: formatDuration(segment.start_ms) });
      row.createEl("p", { text: segment.text ?? "（此段无法识别）" });
    }
    if (snapshot?.provisional) {
      const row = rows.createDiv({ cls: "speech-capture-transcript-row is-provisional" });
      row.createEl("strong", { text: "临时结果" });
      row.createEl("time", { text: formatDuration(snapshot.provisional.start_ms) });
      row.createEl("p", { text: snapshot.provisional.text });
    }
    if (!stable.length && !snapshot?.provisional) {
      rows.createEl("p", {
        cls: "speech-capture-empty-copy",
        text: "稳定文字会在处理过程中逐段出现在这里。"
      });
    }
    const jobId = snapshot?.job.job_id;
    const latestStable = snapshot?.stable_segments.at(-1);
    if (jobId && latestStable) {
      const previous = this.lastAnnouncedStableSegmentByJobId.get(jobId);
      this.lastAnnouncedStableSegmentByJobId.set(jobId, latestStable.segment_id);
      if (previous && previous !== latestStable.segment_id) {
        panel.createDiv({
          cls: "speech-capture-visually-hidden",
          text: `新增稳定逐字稿：${latestStable.text ?? "此段无法识别"}`,
          attr: {
            role: "status",
            "aria-live": "polite",
            "aria-atomic": "true"
          }
        });
      }
    }
    panel.createEl("p", {
      cls: "speech-capture-field__hint",
      text: "处理完成后将生成完整校订逐字稿。"
    });
  }

  private assurance(parent: HTMLElement, iconName: string, text: string): void {
    const item = parent.createDiv({ cls: "speech-capture-assurance" });
    const icon = item.createSpan();
    setIcon(icon, iconName);
    item.createSpan({ text });
  }

  private fact(parent: HTMLElement, name: string, value: string): void {
    const row = parent.createDiv({ cls: "speech-capture-fact" });
    row.createSpan({ text: name });
    row.createSpan({ text: value, attr: { title: value } });
  }

  private async setFile(file: File | null): Promise<void> {
    if (this.submissionState.state === "running") {
      return;
    }
    if (file && !isSupportedAudioFile(file)) {
      this.fileError = "请选择受支持的音频文件";
      this.render();
      return;
    }
    this.fileError = null;
    this.submissionState = { state: "idle" };
    this.draft.file = file;
    this.sourceDurationSeconds = null;
    if (file && !this.recordingDateEdited) {
      const suggestion = suggestRecordingDate(file.name, file.lastModified);
      this.draft.recordingDate = suggestion.value;
      this.recordingDateSource = suggestion.source;
    }
    this.render();
    if (file) {
      const duration = await readAudioDurationSeconds(file);
      if (this.draft.file === file) {
        this.sourceDurationSeconds = duration;
        this.render();
      }
    }
  }

  private setProfile(profile: "accuracy" | "speed"): void {
    if (this.submissionState.state === "running") {
      return;
    }
    this.draft.profile = profile;
    this.render();
  }

  private async toggleSidebar(
    side: "left" | "right",
    collapsed: boolean
  ): Promise<void> {
    await this.plugin.setSidebarCollapsed(side, collapsed);
    this.render();
    const selector = collapsed
      ? side === "left"
        ? ".speech-capture-restore-handle.is-left"
        : ".speech-capture-restore-handle.is-right"
      : side === "left"
        ? ".speech-capture-task-panel .speech-capture-collapse-button"
        : this.viewMode === "task"
          ? this.taskDetailMode === "publication" ||
            this.selectedSnapshot?.job.state === "publishing" ||
            this.selectedSnapshot?.job.state === "published"
            ? ".speech-capture-publication-sidebar .speech-capture-collapse-button"
            : this.selectedSnapshot?.job.state === "processed"
            ? this.taskDetailMode === "review"
              ? ".speech-capture-review-sidebar .speech-capture-collapse-button"
              : ".speech-capture-summary-sidebar .speech-capture-collapse-button"
            : ".speech-capture-current-task .speech-capture-collapse-button"
          : ".speech-capture-confirmation .speech-capture-collapse-button";
    this.contentEl.querySelector<HTMLButtonElement>(selector)?.focus();
  }

  private async refreshWorker(): Promise<void> {
    const worker = this.plugin.preferredWorker();
    if (!worker) {
      this.workerProbe = null;
      this.probingWorker = false;
      this.render();
      return;
    }
    this.probingWorker = true;
    this.render();
    const token = this.plugin.credentials.get(worker.id);
    const result = await probeWorker(new ObsidianWorkerTransport(), worker, token);
    if (this.plugin.preferredWorker()?.id !== worker.id) {
      return;
    }
    this.workerProbe = result;
    this.probingWorker = false;
    this.render();
    if (result.state === "ready" || result.state === "warning") {
      await this.refreshJobs();
    }
  }

  private async pollJobs(): Promise<void> {
    const mode = nextConnectionAttempt(this.connectionRecovery, Date.now());
    if (mode !== null) {
      await this.refreshJobs(mode);
    }
  }

  private async refreshJobs(
    mode: ConnectionAttemptMode = "normal"
  ): Promise<void> {
    if (this.refreshingTasks) {
      return;
    }
    const worker = this.plugin.preferredWorker();
    const vaultId = this.plugin.authorizedVaultId();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (
      !worker ||
      !vaultId ||
      !token ||
      (this.workerProbe?.state !== "ready" && this.workerProbe?.state !== "warning")
    ) {
      return;
    }
    this.refreshingTasks = true;
    const previousJobs = this.jobs;
    const hadTaskError = this.taskError !== null;
    const hadConnectionRecovery = this.connectionRecovery !== null;
    const wasReviewingProcessed = this.selectedSnapshot?.job.state === "processed";
    const previousRevision = this.selectedSnapshot?.job.revision ?? null;
    const previousCorrectionSequence = this.corrections.at(-1)?.sequence ?? 0;
    const previousSummarySignature = this.summaryRevisionSignature();
    try {
      this.jobs = await listJobs(
        new ObsidianWorkerTransport(),
        worker,
        token,
        vaultId
      );
      if (this.selectedJobId) {
        this.selectedSnapshot = await getJobSnapshot(
          new ObsidianWorkerTransport(),
          worker,
          token,
          this.selectedJobId
        );
        this.corrections =
          this.selectedSnapshot.job.state === "processed"
            ? await listJobCorrections(
                new ObsidianWorkerTransport(),
                worker,
                token,
                this.selectedJobId
              )
            : [];
        this.summaryRevisions =
          this.selectedSnapshot.job.state === "processed"
            ? await listJobSummaryRevisions(
                new ObsidianWorkerTransport(),
                worker,
                token,
                this.selectedJobId
              )
            : null;
        if (
          this.selectedSummaryRevisionKey &&
          !this.summaryRevisions?.revisions.some(
            (revision) =>
              revision.revision_key === this.selectedSummaryRevisionKey
          )
        ) {
          this.selectedSummaryRevisionKey = null;
        }
        if (
          !this.selectedSegmentId ||
          !this.selectedSnapshot.stable_segments.some(
            (segment) => segment.segment_id === this.selectedSegmentId
          )
        ) {
          this.selectedSegmentId =
            this.selectedSnapshot.stable_segments.find(
              (segment) => segment.outcome === "transcribed"
            )?.segment_id ?? null;
        }
      }
      this.taskError = null;
      this.connectionRecovery = null;
      if (
        mode === "normal" &&
        !this.selectedJobId &&
        !hadTaskError &&
        !hadConnectionRecovery &&
        sameJobListPresentation(previousJobs, this.jobs)
      ) {
        return;
      }
      if (
        mode === "normal" &&
        wasReviewingProcessed &&
        this.selectedSnapshot?.job.state === "processed" &&
        this.selectedSnapshot.job.revision === previousRevision &&
        (this.corrections.at(-1)?.sequence ?? 0) === previousCorrectionSequence &&
        this.summaryRevisionSignature() === previousSummarySignature
      ) {
        return;
      }
      const selectedState = this.selectedSnapshot?.job.state;
      const shouldPreparePublication =
        (selectedState === "processed" && this.pendingSummaryRevision() === null) ||
        selectedState === "publishing" ||
        selectedState === "published";
      if (shouldPreparePublication && this.publicationState.state === "idle") {
        this.taskDetailMode = "publication";
      }
      this.render();
      if (
        shouldPreparePublication &&
        (this.publicationState.state === "idle" ||
          this.publicationState.state === "waiting_other_client")
      ) {
        window.setTimeout(() => void this.preparePublication(false), 0);
      }
    } catch (error) {
      if (error instanceof JobClientError && error.code === "unavailable") {
        this.connectionRecovery = recoveryAfterFailure(
          this.connectionRecovery,
          mode,
          Date.now()
        );
        this.render();
      } else if (this.viewMode === "task" || mode === "manual") {
        this.taskError =
          error instanceof JobClientError
            ? error.message
            : "暂时无法读取任务进度。";
        this.render();
      }
    } finally {
      this.refreshingTasks = false;
    }
  }

  private async selectJob(jobId: string): Promise<void> {
    this.selectedJobId = jobId;
    this.selectedSnapshot = null;
    this.corrections = [];
    this.summaryRevisions = null;
    this.selectedSummaryRevisionKey = null;
    this.taskDetailMode = "review";
    this.publicationState = { state: "idle" };
    this.publicationBusy = false;
    this.selectedSegmentId = null;
    this.speakerFilterId = null;
    this.speakerSearch = "";
    this.taskError = null;
    this.connectionRecovery = null;
    this.viewMode = "task";
    this.render();
    await this.refreshJobs();
  }

  private openIntake(): void {
    this.releaseReviewAudioUrl();
    this.viewMode = "intake";
    this.selectedJobId = null;
    this.selectedSnapshot = null;
    this.corrections = [];
    this.summaryRevisions = null;
    this.selectedSummaryRevisionKey = null;
    this.taskDetailMode = "review";
    this.publicationState = { state: "idle" };
    this.publicationBusy = false;
    this.selectedSegmentId = null;
    this.speakerFilterId = null;
    this.taskError = null;
    this.connectionRecovery = null;
    this.render();
  }

  private selectedReviewSegment(): TranscriptSegmentSchema | null {
    const segments = this.selectedSnapshot?.stable_segments ?? [];
    return (
      segments.find((segment) => segment.segment_id === this.selectedSegmentId) ??
      segments.find((segment) => segment.outcome === "transcribed") ??
      null
    );
  }

  private pendingSummaryRevision(): SummaryRevisionSchema | null {
    return (
      [...(this.summaryRevisions?.revisions ?? [])]
        .reverse()
        .find((revision) => revision.status === "pending") ?? null
    );
  }

  private selectedSummaryRevision(): SummaryRevisionSchema | null {
    const revisions = this.summaryRevisions?.revisions ?? [];
    return (
      revisions.find(
        (revision) => revision.revision_key === this.selectedSummaryRevisionKey
      ) ??
      this.pendingSummaryRevision() ??
      revisions.at(-1) ??
      null
    );
  }

  private summaryRevisionSignature(): string {
    return (this.summaryRevisions?.revisions ?? [])
      .map((revision) => `${revision.revision_key}:${revision.status}`)
      .join("|");
  }

  private isNarrowWorkbench(): boolean {
    return (
      workbenchLayoutSize(this.workbenchWidth || this.contentEl.clientWidth) ===
      "narrow"
    );
  }

  private reviewSpeakerIds(): string[] {
    const values = new Set<string>();
    for (const segment of this.selectedSnapshot?.stable_segments ?? []) {
      if (segment.speaker_id) {
        values.add(segment.speaker_id);
      }
      const effective = effectiveTranscriptSegment(segment, this.corrections);
      if (effective.speakerId) {
        values.add(effective.speakerId);
      }
    }
    return [...values].sort((left, right) => left.localeCompare(right));
  }

  private selectReviewSegment(
    segment: TranscriptSegmentSchema | undefined,
    focusInlineEditor = false
  ): void {
    if (!segment) {
      return;
    }
    this.selectedSegmentId = segment.segment_id;
    this.speakerSearch = "";
    this.render();
    if (focusInlineEditor) {
      this.contentEl
        .querySelector<HTMLElement>(
          ".speech-capture-segment-editor.is-inline h3[tabindex='-1']"
        )
        ?.focus();
    }
  }

  private segmentReviewDraftKey(segmentId: string): string {
    return segmentReviewDraftKey(
      this.selectedSnapshot?.job.job_id ?? this.selectedJobId ?? "unknown",
      segmentId
    );
  }

  private hasUnsavedSelectedReviewDraft(): boolean {
    const selected = this.selectedReviewSegment();
    if (!selected) {
      return false;
    }
    return this.segmentReviewDrafts.has(
      this.segmentReviewDraftKey(selected.segment_id)
    );
  }

  private focusSelectedReviewRow(): void {
    const selectedId = this.selectedSegmentId;
    if (!selectedId) {
      return;
    }
    const rows = this.contentEl.querySelectorAll<HTMLButtonElement>(
      ".speech-capture-review-row[data-segment-id]"
    );
    for (const row of rows) {
      if (row.dataset.segmentId === selectedId) {
        row.focus();
        return;
      }
    }
  }

  private selectSpeakerFilter(speakerId: string | null): void {
    this.speakerFilterId = speakerId;
    if (speakerId !== null) {
      const first = this.selectedSnapshot?.stable_segments.find(
        (segment) =>
          segment.outcome === "transcribed" &&
          effectiveTranscriptSegment(segment, this.corrections).speakerId === speakerId
      );
      if (first) {
        this.selectedSegmentId = first.segment_id;
      }
    }
    this.speakerSearch = "";
    this.render();
  }

  private speakerPrimaryLabel(speakerId: string): string {
    const current = effectiveSpeakerDisplayName(speakerId, this.corrections);
    return current.revised ? current.displayName : speakerLabel(speakerId);
  }

  private speakerOptionLabel(speakerId: string): string {
    const current = effectiveSpeakerDisplayName(speakerId, this.corrections);
    return current.revised
      ? `${current.displayName} · ${speakerLabel(speakerId)}`
      : speakerLabel(speakerId);
  }

  private async saveSegmentReview(
    segment: TranscriptSegmentSchema,
    beforeText: string,
    beforeSpeakerId: string | null,
    afterText: string,
    afterSpeakerId: string | null
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.reviewSaving) {
      return;
    }
    const shouldRestoreRowFocus = this.isNarrowWorkbench();
    this.reviewSaving = true;
    this.taskError = null;
    this.render();
    try {
      await reviewTranscriptSegment(
        new ObsidianWorkerTransport(),
        worker,
        token,
        {
          job,
          segmentId: segment.segment_id,
          beforeText,
          afterText,
          beforeSpeakerId,
          afterSpeakerId
        }
      );
      this.reviewSaving = false;
      this.segmentReviewDrafts.delete(
        this.segmentReviewDraftKey(segment.segment_id)
      );
      await this.refreshJobs();
      if (shouldRestoreRowFocus) {
        this.focusSelectedReviewRow();
      }
    } catch (error) {
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "这段修订未能保存，请重新读取后再试。";
      if (error instanceof JobClientError && error.code === "conflict") {
        await this.refreshJobs();
      } else {
        this.render();
      }
    } finally {
      this.reviewSaving = false;
    }
  }

  private async saveSpeakerDisplayName(
    speakerId: string,
    before: string,
    after: string
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.speakerRenameSaving || !after) {
      return;
    }
    this.speakerRenameSaving = true;
    this.taskError = null;
    this.render();
    try {
      await renameJobSpeakerDisplayName(
        new ObsidianWorkerTransport(),
        worker,
        token,
        { job, speakerId, before, after }
      );
      this.speakerRenameSaving = false;
      await this.refreshJobs();
    } catch (error) {
      this.speakerRenameSaving = false;
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "说话人显示名未能保存，请重新读取后再试。";
      if (error instanceof JobClientError && error.code === "conflict") {
        await this.refreshJobs();
      } else {
        this.render();
      }
    }
  }

  private async saveSummaryDecision(
    revision: SummaryRevisionSchema,
    decision: "accepted" | "rejected"
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.summaryDecisionSaving) {
      return;
    }
    this.summaryDecisionSaving = true;
    this.taskError = null;
    this.render();
    try {
      await decideJobSummaryRevision(
        new ObsidianWorkerTransport(),
        worker,
        token,
        {
          job,
          revisionKey: revision.revision_key,
          decision
        }
      );
      this.summaryDecisionSaving = false;
      await this.refreshJobs();
    } catch (error) {
      this.summaryDecisionSaving = false;
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "笔记版本未能保存，请重新读取后再试。";
      if (error instanceof JobClientError && error.code === "conflict") {
        await this.refreshJobs();
      } else {
        this.render();
      }
    }
  }

  private async regenerateSummary(): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.summaryRegenerating) {
      return;
    }
    this.summaryRegenerating = true;
    this.taskError = null;
    this.render();
    try {
      const result = await regenerateJobSummary(
        new ObsidianWorkerTransport(),
        worker,
        token,
        job
      );
      this.selectedSummaryRevisionKey = result.revision.revision_key;
      this.taskDetailMode = "summary";
      this.summaryRegenerating = false;
      await this.refreshJobs();
    } catch (error) {
      this.summaryRegenerating = false;
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "笔记未能重新生成，当前 Note 保持不变。";
      if (error instanceof JobClientError && error.code === "conflict") {
        await this.refreshJobs();
      } else {
        this.render();
      }
    }
  }

  private async preparePublication(force: boolean): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.publicationBusy) {
      return;
    }
    if (!force && this.publicationState.state === "conflict") {
      return;
    }
    const keepWaitingView =
      !force && this.publicationState.state === "waiting_other_client";
    this.publicationBusy = true;
    this.taskDetailMode = "publication";
    if (!keepWaitingView) {
      this.publicationState = { state: "loading" };
      this.render();
    }
    const transport = new ObsidianWorkerTransport();
    try {
      const status = await getPublicationStatus(
        transport,
        worker,
        token,
        job.job_id,
        this.plugin.settings.outputFolder
      );
      if (status.receipt || status.job.state === "published") {
        const target = status.receipt?.target_relative_path ?? status.suggested_target_relative_path;
        this.publicationState = { state: "published", targetRelativePath: target };
        this.publicationBusy = false;
        this.render();
        return;
      }
      if (status.active_lease && !status.active_lease.owned_by_caller) {
        this.publicationState = {
          state: "waiting_other_client",
          targetRelativePath: status.active_lease.target_relative_path
        };
        this.publicationBusy = false;
        this.render();
        return;
      }
      const packageData = await downloadPublicationPackage(
        transport,
        worker,
        token,
        job.job_id
      );
      if (packageData.manifestSha256 !== status.manifest_sha256) {
        throw new VaultPublicationError("verification", "Worker 发布清单已发生变化。");
      }
      const target =
        status.active_lease?.target_relative_path ?? status.suggested_target_relative_path;
      const inspection = await inspectPublicationTarget(
        this.app.vault.adapter,
        target,
        packageData
      );
      if (inspection.kind === "conflict") {
        this.publicationState = {
          state: "conflict",
          status,
          packageData,
          diff: inspection.diff,
          viewed: false
        };
        this.publicationBusy = false;
        this.render();
        return;
      }
      await this.publishPreparedPackage(
        status,
        packageData,
        target,
        status.active_lease?.lease_id ?? null
      );
    } catch (error) {
      this.publicationBusy = false;
      if (error instanceof PublicationClientError && error.kind === "lease") {
        this.publicationState = {
          state: "waiting_other_client",
          targetRelativePath: this.plugin.settings.outputFolder
        };
      } else {
        this.publicationState = {
          state: "error",
          message:
            error instanceof PublicationClientError || error instanceof VaultPublicationError
              ? error.message
              : "写入后的完整性检查未通过，现有内容没有被覆盖。"
        };
      }
      this.render();
    }
  }

  private async publishPreparedPackage(
    status: PublicationStatusResponse,
    packageData: DownloadedPublicationPackage,
    targetRelativePath: string,
    existingLeaseId: string | null
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (!worker || !token) {
      return;
    }
    const transport = new ObsidianWorkerTransport();
    let leaseId = existingLeaseId;
    try {
      if (!leaseId) {
        const claim = await claimPublication(transport, worker, token, {
          job: status.job,
          targetRelativePath,
          manifestSha256: packageData.manifestSha256
        });
        leaseId = claim.lease.lease_id;
      }
      this.publicationState = { state: "publishing", targetRelativePath };
      this.render();
      await writePublicationPackage(this.app.vault.adapter, {
        targetRelativePath,
        leaseId,
        packageData
      });
      const acknowledged = await acknowledgePublication(transport, worker, token, {
        jobId: status.job.job_id,
        leaseId,
        manifestSha256: packageData.manifestSha256
      });
      if (this.selectedSnapshot) {
        this.selectedSnapshot = { ...this.selectedSnapshot, job: acknowledged.job };
      }
      this.jobs = this.jobs.map((item) =>
        item.job_id === acknowledged.job.job_id ? acknowledged.job : item
      );
      this.publicationState = { state: "published", targetRelativePath };
      this.publicationBusy = false;
      this.render();
    } catch (error) {
      if (leaseId) {
        try {
          await releasePublication(
            transport,
            worker,
            token,
            status.job.job_id,
            leaseId
          );
        } catch {
          // The lease expires safely if the release response cannot be completed.
        }
      }
      if (error instanceof VaultPublicationError && error.kind === "conflict") {
        const inspection = await inspectPublicationTarget(
          this.app.vault.adapter,
          targetRelativePath,
          packageData
        );
        if (
          targetRelativePath === status.suggested_target_relative_path &&
          inspection.kind === "conflict"
        ) {
          this.publicationState = {
            state: "conflict",
            status,
            packageData,
            diff: inspection.diff,
            viewed: false
          };
          this.publicationBusy = false;
          this.render();
          return;
        }
      }
      throw error;
    }
  }

  private async savePublicationToNewLocation(
    conflict: Extract<PublicationViewState, { state: "conflict" }>
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.publicationBusy) {
      return;
    }
    this.publicationBusy = true;
    this.render();
    try {
      const transport = new ObsidianWorkerTransport();
      const status = await getPublicationStatus(
        transport,
        worker,
        token,
        job.job_id,
        this.plugin.settings.outputFolder
      );
      if (status.manifest_sha256 !== conflict.packageData.manifestSha256) {
        throw new VaultPublicationError("verification", "Worker 发布清单已发生变化，请重新查看。");
      }
      const target = await chooseNewPublicationPath(
        this.app.vault.adapter,
        conflict.status.suggested_target_relative_path
      );
      await this.publishPreparedPackage(status, conflict.packageData, target, null);
    } catch (error) {
      this.publicationBusy = false;
      this.publicationState = {
        state: "error",
        message:
          error instanceof PublicationClientError || error instanceof VaultPublicationError
            ? error.message
            : "新位置未能完成写入，当前内容没有被覆盖。"
      };
      this.render();
    }
  }

  private async openPublishedNote(targetRelativePath: string): Promise<void> {
    const notePath = `${targetRelativePath}/note.md`;
    await this.app.workspace.openLinkText(notePath, "", false);
  }

  private async playReviewSegment(
    audio: HTMLAudioElement,
    jobId: string,
    segment: TranscriptSegmentSchema,
    button: HTMLButtonElement
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (!worker || !token) {
      return;
    }
    button.disabled = true;
    this.releaseReviewAudioUrl();
    try {
      const local = this.localAudioByJobId.get(jobId);
      if (local) {
        this.reviewAudioUrl = URL.createObjectURL(local);
        audio.src = this.reviewAudioUrl;
        audio.addEventListener(
          "loadedmetadata",
          () => {
            audio.currentTime = segment.start_ms / 1_000;
            void audio.play();
          },
          { once: true }
        );
        const stopAtEnd = (): void => {
          if (audio.currentTime >= segment.end_ms / 1_000) {
            audio.pause();
            audio.removeEventListener("timeupdate", stopAtEnd);
          }
        };
        audio.addEventListener("timeupdate", stopAtEnd);
      } else {
        const loaded = await loadReviewAudioSegment(
          new ObsidianWorkerTransport(),
          worker,
          token,
          jobId,
          segment.start_ms,
          segment.end_ms
        );
        this.reviewAudioUrl = URL.createObjectURL(loaded.blob);
        audio.src = this.reviewAudioUrl;
        await audio.play();
      }
    } catch (error) {
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "当前片段的音频暂时无法播放。";
      this.render();
    } finally {
      button.disabled = false;
    }
  }

  private releaseReviewAudioUrl(): void {
    if (this.reviewAudioUrl) {
      URL.revokeObjectURL(this.reviewAudioUrl);
      this.reviewAudioUrl = null;
    }
  }

  private selectedJob(): JobSchema | null {
    return this.jobs.find((job) => job.job_id === this.selectedJobId) ?? null;
  }

  private async performTaskAction(
    action: "pause" | "resume" | "cancel" | "retry"
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job ?? this.selectedJob();
    if (!worker || !token || !job) {
      return;
    }
    try {
      await applyJobAction(
        new ObsidianWorkerTransport(),
        worker,
        token,
        job,
        action
      );
      await this.refreshJobs();
    } catch (error) {
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "任务操作未完成，请重新读取后再试。";
      await this.refreshJobs();
    }
  }

  private openLocalWorkerHelp(): void {
    const modal = new Modal(this.app);
    modal.titleEl.setText("这台 Mac 的 Worker 尚未就绪");
    modal.contentEl.createEl("p", {
      text: "仅凭 Obsidian 当前无法判断 Worker 尚未安装、服务没有启动，还是模型或空间条件尚未完成。"
    });
    const steps = modal.contentEl.createEl("ol");
    steps.createEl("li", {
      text: "在这台 Mac 上打开 Speech Capture Worker Manager。"
    });
    steps.createEl("li", {
      text: "按其中提示完成安装或启动，并处理模型、磁盘或内存提醒。"
    });
    steps.createEl("li", {
      text: "回到 Obsidian 后点击“重新检测”；当前任务和草稿不会切换到其他设备。"
    });
    const close = modal.contentEl.createEl("button", {
      cls: "mod-cta",
      text: "知道了",
      attr: { type: "button" }
    });
    close.addEventListener("click", () => modal.close());
    modal.open();
    close.focus();
  }

  private openCancelConfirmation(): void {
    const job = this.selectedSnapshot?.job ?? this.selectedJob();
    if (!job || !canCancelJob(job.state)) {
      return;
    }
    const modal = new Modal(this.app);
    modal.titleEl.setText("取消这个任务？");
    modal.contentEl.createEl("p", {
      text: "Worker 将停止后续处理。已上传音频、稳定逐字稿和处理检查点会继续保留；任务取消后不能恢复。"
    });
    const actions = modal.contentEl.createDiv({
      cls: "speech-capture-confirm-actions"
    });
    const keep = actions.createEl("button", {
      text: "继续保留任务",
      attr: { type: "button" }
    });
    keep.addEventListener("click", () => modal.close());
    const confirm = actions.createEl("button", {
      cls: "mod-warning",
      text: "确认取消任务",
      attr: { type: "button" }
    });
    confirm.addEventListener("click", () => {
      modal.close();
      void this.performTaskAction("cancel");
    });
    modal.open();
    keep.focus();
  }

  private openPairing(): void {
    this.viewMode = "pairing";
    this.pairingState = { state: "idle" };
    this.render();
    this.contentEl.querySelector<HTMLInputElement>("[aria-label='配对码']")?.focus();
  }

  private closePairing(): void {
    this.pairingTicket = "";
    this.pairingState = { state: "idle" };
    this.viewMode = "intake";
    this.render();
  }

  private async submitPairing(): Promise<void> {
    const worker = this.plugin.preferredWorker();
    if (!worker) {
      this.pairingState = { state: "error", message: "未检测到可连接的 Worker" };
      this.render();
      return;
    }
    this.pairingState = { state: "submitting" };
    this.render();
    const result = await confirmPairingTicket(
      new ObsidianWorkerTransport(),
      worker,
      this.pairingTicket
    );
    if (!result.ok) {
      this.pairingState = {
        state: "error",
        message: pairingErrorMessage(result.reason)
      };
      this.render();
      return;
    }
    try {
      await this.plugin.setAuthorizedVaultId(result.credential.allowed_vault_ids[0]!);
      this.plugin.credentials.set(worker.id, result.credential.bearer_token);
    } catch {
      this.pairingState = {
        state: "error",
        message: "Worker 已接受授权，但本机未能完整保存。请重新检测；若仍未连接，请在 Worker Manager 中撤销旧授权后重新配对"
      };
      this.render();
      return;
    }
    this.pairingTicket = "";
    this.pairingState = { state: "idle" };
    this.viewMode = "intake";
    this.workerProbe = null;
    await this.refreshWorker();
  }

  private selectedProfileCanStart(): boolean {
    if (
      this.workerProbe?.state !== "ready" &&
      this.workerProbe?.state !== "warning"
    ) {
      return false;
    }
    return (
      this.workerProbe.readiness.profiles.find(
        (profile) => profile.model_profile === this.draft.profile
      )?.can_start ?? false
    );
  }

  private async submitDraft(): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const file = this.draft.file;
    const vaultId = this.plugin.authorizedVaultId();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (!worker || !file || !vaultId || !token || !this.selectedProfileCanStart()) {
      this.submissionState = {
        state: "error",
        message: "提交条件已变化，请重新检测 Worker 后再试"
      };
      this.render();
      this.focusSubmissionError();
      return;
    }
    const diskEstimate = estimateJobDiskBytes(file.size, this.sourceDurationSeconds);
    const readiness =
      this.workerProbe?.state === "ready" || this.workerProbe?.state === "warning"
        ? this.workerProbe.readiness
        : null;
    if (
      diskEstimate !== null &&
      readiness !== null &&
      readiness.disk_free_bytes - diskEstimate.totalBytes < readiness.disk_reserve_bytes
    ) {
      this.submissionState = {
        state: "error",
        message: "Worker 磁盘空间不足，无法在保留安全余量的前提下上传并处理此文件"
      };
      this.render();
      this.focusSubmissionError();
      return;
    }
    this.submissionState = {
      state: "running",
      progress: emptySubmissionProgress(file.size)
    };
    this.render();
    try {
      const result = await submitRecording(new ObsidianWorkerTransport(), {
        worker,
        bearerToken: token,
        vaultId,
        source: file,
        recordingDate: this.draft.recordingDate,
        recordingContext: this.draft.context,
        modelProfile: this.draft.profile,
        ...(this.draft.contentType === "auto"
          ? {}
          : { contentTypeOverride: this.draft.contentType }),
        onProgress: (progress) => {
          this.submissionState = { state: "running", progress };
          this.render();
        }
      });
      this.localAudioByJobId.set(result.job.job_id, file);
      this.submissionState = { state: "complete", jobId: result.job.job_id };
      this.jobs = [
        result.job,
        ...this.jobs.filter((job) => job.job_id !== result.job.job_id)
      ];
      this.selectedJobId = result.job.job_id;
      this.selectedSnapshot = null;
      this.viewMode = "task";
      this.render();
      await this.refreshJobs();
    } catch (error) {
      this.submissionState = {
        state: "error",
        message:
          error instanceof SubmissionError
            ? error.message
            : "提交未完成，已上传的分段仍保留在 Worker，请重试"
      };
      this.render();
      this.focusSubmissionError();
    }
  }

  private renderSubmissionStatus(parent: HTMLElement): void {
    const status = parent.createDiv({
      cls: `speech-capture-submission is-${this.submissionState.state}`,
      attr: {
        role: this.submissionState.state === "error" ? "alert" : "status",
        ...(this.submissionState.state === "error" ? { tabindex: "-1" } : {})
      }
    });
    if (this.submissionState.state === "running") {
      const progress = this.submissionState.progress;
      const percent = Math.round(
        (progress.processedBytes / Math.max(1, progress.totalBytes)) * 100
      );
      const row = status.createDiv({ cls: "speech-capture-submission__row" });
      row.createEl("strong", { text: submissionPhaseLabel(progress.phase) });
      row.createSpan({ text: `${percent}%` });
      const track = status.createDiv({
        cls: "speech-capture-progress",
        attr: {
          role: "progressbar",
          "aria-label": "音频提交进度",
          "aria-valuemin": "0",
          "aria-valuemax": "100",
          "aria-valuenow": percent.toString(),
          "aria-valuetext": `${percent.toString()}%`
        }
      });
      track.createDiv({
        cls: "speech-capture-progress__fill",
        attr: { style: `width: ${percent}%` }
      });
      status.createEl("p", {
        text:
          progress.phase === "waiting_retry"
            ? `连接中断，1 分钟后自动续传（${(progress.retryAttempt ?? 1).toString()}/${(progress.retryLimit ?? 3).toString()}）`
            : progress.phase === "uploading"
            ? `已确认 ${progress.completedParts}/${progress.totalParts} 个分段`
            : "请保持 Obsidian 打开，上传完成后即可关闭"
      });
      return;
    }
    if (this.submissionState.state === "complete") {
      status.createEl("strong", { text: "任务已安全提交" });
      status.createEl("p", { text: "Worker 已开始处理，现在可以关闭 Obsidian。" });
      return;
    }
    if (this.submissionState.state === "error") {
      status.createEl("strong", { text: "提交尚未完成" });
      status.createEl("p", { text: this.submissionState.message });
    }
  }

  private focusSubmissionError(): void {
    this.contentEl
      .querySelector<HTMLElement>(
        ".speech-capture-submission.is-error[tabindex='-1']"
      )
      ?.focus();
  }

  private resetDraft(): void {
    if (this.submissionState.state === "running") {
      return;
    }
    this.fileError = null;
    this.sourceDurationSeconds = null;
    this.recordingDateEdited = false;
    this.recordingDateSource = "today";
    this.submissionState = { state: "idle" };
    this.draft = {
      file: null,
      recordingDate: localDate(new Date()),
      context: "",
      profile: this.plugin.settings.preferredProfile,
      contentType: "auto"
    };
    this.render();
  }

  private workerStatusPresentation(workerName?: string): {
    text: string;
    className: string;
  } {
    if (!workerName) {
      return { text: "未配置 Worker", className: "is-warning" };
    }
    if (this.probingWorker) {
      return { text: `${workerName} · 正在检测`, className: "is-neutral" };
    }
    if (this.connectionRecovery) {
      return { text: `${workerName} · 连接中断`, className: "is-warning" };
    }
    if (this.viewMode === "task" && this.taskDetailMode === "publication") {
      switch (this.publicationState.state) {
        case "conflict":
          return { text: "发现发布冲突", className: "is-warning" };
        case "publishing":
          return { text: "正在写入 Obsidian", className: "is-active" };
        case "waiting_other_client":
          return { text: "等待另一台设备发布", className: "is-neutral" };
        case "published":
          return { text: "已发布到 Obsidian", className: "is-good" };
        default:
          break;
      }
    }
    if (this.viewMode === "task") {
      const job = this.selectedSnapshot?.job ?? this.selectedJob();
      if (job && isActiveJob(job.state)) {
        return { text: `${workerName} · 正在处理`, className: "is-active" };
      }
    }
    switch (this.workerProbe?.state) {
      case "ready":
        return { text: `${workerName} · 已就绪`, className: "is-good" };
      case "warning":
        return { text: `${workerName} · 已连接 · 需注意`, className: "is-warning" };
      case "blocked":
        return { text: `${workerName} · 尚未准备好`, className: "is-warning" };
      case "pairing_required":
        return { text: `${workerName} · 需要连接`, className: "is-warning" };
      case "incompatible":
        return { text: `${workerName} · 版本不兼容`, className: "is-warning" };
      case "unreachable":
        return { text: `${workerName} · 未检测到可用 Worker`, className: "is-warning" };
      default:
        return { text: `${workerName} · 等待检测`, className: "is-neutral" };
    }
  }

  private taskCardStatus(job: JobSchema): string {
    if (job.job_id !== this.selectedJobId) {
      return jobStateLabel(job.state);
    }
    if (this.pendingSummaryRevision()) {
      return "等待确认";
    }
    switch (this.publicationState.state) {
      case "conflict":
        return "发布冲突";
      case "loading":
        return "等待发布";
      case "publishing":
        return "正在发布";
      case "waiting_other_client":
        return "等待发布";
      case "published":
        return "已发布";
      default:
        return jobStateLabel(job.state);
    }
  }
}

function publicationTargetPath(state: PublicationViewState): string | null {
  switch (state.state) {
    case "publishing":
    case "waiting_other_client":
    case "published":
      return state.targetRelativePath;
    case "conflict":
      return state.status.suggested_target_relative_path;
    default:
      return null;
  }
}

function localDate(date: Date): string {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function pairingErrorMessage(
  reason: "invalid" | "expired" | "conflict" | "unavailable"
): string {
  switch (reason) {
    case "invalid":
      return "配对码不正确，请检查后重试";
    case "expired":
      return "配对码已失效，请在目标 Mac 上生成新配对码";
    case "conflict":
      return "此设备已有有效授权，请重新检测或先在 Worker Manager 中撤销旧授权";
    case "unavailable":
      return "暂时无法完成连接，当前设置和任务草稿仍已保留";
  }
}

function emptySubmissionProgress(totalBytes: number): SubmissionProgress {
  return {
    phase: "hashing",
    processedBytes: 0,
    totalBytes,
    completedParts: 0,
    totalParts: 0
  };
}

function submissionButtonLabel(state: SubmissionState): string {
  switch (state.state) {
    case "running":
      return "正在提交";
    case "complete":
      return "已提交";
    case "error":
      return "重试提交";
    case "idle":
      return "确认并开始上传";
  }
}

function submissionPhaseLabel(phase: SubmissionProgress["phase"]): string {
  switch (phase) {
    case "hashing":
      return "正在安全读取音频";
    case "uploading":
      return "正在上传音频";
    case "waiting_retry":
      return "等待自动续传";
    case "verifying":
      return "正在核对完整性";
    case "creating_job":
      return "正在创建任务";
    case "done":
      return "提交完成";
  }
}

function taskTitle(filename: string): string {
  return filename.replace(/\.[^.]+$/, "") || filename;
}

function summaryRevisionStatusLabel(
  status: SummaryRevisionSchema["status"]
): string {
  return {
    pending: "等待确认",
    accepted: "已采用",
    rejected: "未采用"
  }[status];
}

function summaryChangeKindLabel(
  kind: "added" | "modified" | "removed"
): string {
  return {
    added: "新增",
    modified: "已修改",
    removed: "已移除"
  }[kind];
}

function manualSectionBody(markdown: string): string {
  return markdown
    .replace(/^## 我的补充\s*/u, "")
    .trim();
}

function formatSummaryTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(parsed);
}

function jobStateLabel(state: JobSchema["state"]): string {
  return (
    {
      created: "已创建",
      uploading: "正在上传",
      verifying: "正在验证",
      queued: "排队中",
      preprocessing: "正在预处理",
      transcribing: "正在转写",
      aligning: "正在对齐",
      diarizing: "正在识别说话人",
      structuring: "正在提炼",
      quality_check: "质量检查",
      processed: "处理完成",
      publishing: "正在发布",
      published: "已发布",
      paused: "已安全暂停",
      waiting_user: "等待确认",
      partial: "部分完成",
      failed: "处理失败",
      cancelled: "已取消"
    } satisfies Record<JobSchema["state"], string>
  )[state];
}

function jobStateTone(state: JobSchema["state"]): "good" | "active" | "warning" {
  if (state === "processed" || state === "published") {
    return "good";
  }
  if (
    state === "paused" ||
    state === "waiting_user" ||
    state === "partial" ||
    state === "failed" ||
    state === "cancelled"
  ) {
    return "warning";
  }
  return "active";
}

function isPausable(state: JobSchema["state"]): boolean {
  return [
    "queued",
    "preprocessing",
    "transcribing",
    "aligning",
    "diarizing",
    "structuring",
    "quality_check"
  ].includes(state);
}

function isActiveJob(state: JobSchema["state"]): boolean {
  return [
    "created",
    "uploading",
    "verifying",
    "queued",
    "preprocessing",
    "transcribing",
    "aligning",
    "diarizing",
    "structuring",
    "quality_check",
    "publishing"
  ].includes(state);
}

function formatDuration(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1_000));
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")}`
    : `${minutes.toString().padStart(2, "0")}:${seconds.toString().padStart(2, "0")}`;
}

function speakerLabel(speakerId: string | null): string {
  if (!speakerId) {
    return "说话人待识别";
  }
  const suffix = speakerId.match(/(\d+)$/)?.[1];
  return suffix ? `说话人 ${Number(suffix) + 1}` : "说话人";
}
