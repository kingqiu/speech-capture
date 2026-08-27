import {
  ItemView,
  Modal,
  normalizePath,
  setIcon,
  setTooltip,
  TFile,
  TFolder,
  type WorkspaceLeaf
} from "obsidian";

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
  deleteJob,
  deleteJobSourceAudio,
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
  reviewTranscriptSegment,
  saveJobSummaryRevisionDraft
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
  structuringProgressPresentation,
  taskStatePresentation
} from "./task-state-presentation";
import {
  isCurrentTaskRequest,
  isReviewableJobState,
  taskSurface,
  type TaskDetailMode
} from "./task-view-routing";
import {
  canManageJobData,
  requiresPublishedFolderCleanup,
  safePublishedFolderPath
} from "./record-management";
import {
  confirmPairingTicket,
  probeWorker,
  type WorkerProbeResult
} from "./worker-probe";
import { workbenchLayoutSize } from "./workbench-layout";
import {
  buildSummaryChanges,
  countSummaryChanges,
  renderSummaryCandidateMarkdown
} from "./summary-diff";
import {
  currentPublicationReceipt,
  publishedManifestIsStale,
  summaryRevisionIsPublished,
  upsertSavedSummaryRevision
} from "./summary-publication";
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

const SCROLL_REGION_SELECTORS = Object.freeze([
  ".speech-capture-task-panel",
  ".speech-capture-intake",
  ".speech-capture-confirmation",
  ".speech-capture-active-task",
  ".speech-capture-current-task",
  ".speech-capture-review",
  ".speech-capture-review-sidebar",
  ".speech-capture-summary-diff",
  ".speech-capture-summary-history",
  ".speech-capture-summary-sidebar",
  ".speech-capture-publication",
  ".speech-capture-publication-sidebar"
] as const);

interface WorkbenchScrollState {
  readonly root: number;
  readonly regions: ReadonlyMap<string, number>;
  readonly focus: WorkbenchFocusState | null;
}

interface WorkbenchFocusState {
  readonly tagName: string;
  readonly ariaLabel: string | null;
  readonly segmentId: string | null;
  readonly placeholder: string | null;
  readonly text: string | null;
}

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

type PublicationConflictStep = "notice" | "difference" | "location";

interface PublicationReplacementContext {
  readonly previousTargetRelativePath: string;
  readonly publicationVersion: number;
}

type PublicationViewState =
  | { readonly state: "idle" }
  | { readonly state: "loading" }
  | {
      readonly state: "publishing";
      readonly targetRelativePath: string;
      readonly replacement?: PublicationReplacementContext | undefined;
    }
  | { readonly state: "waiting_other_client"; readonly targetRelativePath: string }
  | {
      readonly state: "conflict";
      readonly status: PublicationStatusResponse;
      readonly packageData: DownloadedPublicationPackage;
      readonly diff: PublicationConflictDiff;
      readonly step: PublicationConflictStep;
      readonly recommendedTargetRelativePath: string;
      readonly destinationChoice: "recommended" | "custom";
      readonly customTargetRelativePath: string;
      readonly pathError?: string | undefined;
    }
  | {
      readonly state: "published";
      readonly targetRelativePath: string;
      readonly manifestSha256: string;
      readonly replacement?: PublicationReplacementContext | undefined;
    }
  | { readonly state: "error"; readonly message: string };

export class SpeechWorkbenchView extends ItemView {
  private viewMode: "intake" | "pairing" | "task" = "intake";
  private taskDetailMode: TaskDetailMode = "review";
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
  private summaryDraftOpening = false;
  private summaryDraftEditing = false;
  private summaryDraftText = "";
  private summaryDraftSaving = false;
  private summaryDraftFeedback: string | null = null;
  private summaryRegenerating = false;
  private summaryRegenerationStartedAt: number | null = null;
  private summaryRegenerationTimer: number | null = null;
  private summaryRegenerationFeedback:
    | { readonly state: "working" | "error"; readonly message: string }
    | null = null;
  private publicationState: PublicationViewState = { state: "idle" };
  private publicationBusy = false;
  private taskSelectionEpoch = 0;
  private refreshQueued = false;
  private allowAutomaticPublicationView = true;
  private selectedSegmentId: string | null = null;
  private speakerFilterId: string | null = null;
  private speakerSearch = "";
  private reviewSaving = false;
  private speakerRenameSaving = false;
  private speakerRenameFeedback:
    | {
        readonly state: "saving" | "slow" | "success" | "error";
        readonly message: string;
      }
    | null = null;
  private speakerRenameSlowTimer: number | null = null;
  private reviewMutationEpoch = 0;
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
    this.clearSpeakerRenameSlowTimer();
    this.stopSummaryRegenerationTimer();
    this.releaseReviewAudioUrl();
  }

  public async onWorkerSettingsChanged(): Promise<void> {
    this.stopSummaryRegenerationTimer();
    this.taskSelectionEpoch += 1;
    this.workerProbe = null;
    this.probingWorker = false;
    this.pairingTicket = "";
    this.pairingState = { state: "idle" };
    this.jobs = [];
    this.selectedJobId = null;
    this.selectedSnapshot = null;
    this.corrections = [];
    this.summaryRevisions = null;
    this.summaryDecisionSaving = false;
    this.resetSummaryDraftEditor();
    this.summaryRegenerating = false;
    this.summaryRegenerationStartedAt = null;
    this.summaryRegenerationFeedback = null;
    this.publicationState = { state: "idle" };
    this.publicationBusy = false;
    this.allowAutomaticPublicationView = true;
    this.connectionRecovery = null;
    this.viewMode = "intake";
    this.render();
    await this.refreshWorker();
  }

  private render(): void {
    const scrollState = this.captureScrollState();
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
      const selectedState =
        this.selectedSnapshot?.job.state ?? this.selectedJob()?.state ?? null;
      const surface = taskSurface(this.taskDetailMode, selectedState);
      if (surface === "publication") {
        this.renderPublication(layout);
        this.renderPublicationSidebar(layout);
      } else if (surface === "review") {
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
        layout.addClass("is-merged-detail");
        this.renderActiveTask(layout);
      }
      this.renderRestoreHandles(layout);
    } else {
      layout.addClass("is-merged-detail");
      this.renderTaskSidebar(layout);
      const intake = this.renderIntake(layout);
      this.renderConfirmation(intake);
      this.renderRestoreHandles(layout);
    }
    this.restoreScrollState(scrollState);
  }

  private captureScrollState(): WorkbenchScrollState {
    const regions = new Map<string, number>();
    for (const selector of SCROLL_REGION_SELECTORS) {
      const element = this.contentEl.querySelector<HTMLElement>(selector);
      if (element) {
        regions.set(selector, element.scrollTop);
      }
    }
    return {
      root: this.contentEl.scrollTop,
      regions,
      focus: this.captureFocusState()
    };
  }

  private restoreScrollState(state: WorkbenchScrollState): void {
    this.restoreFocusState(state.focus);
    this.contentEl.scrollTop = state.root;
    for (const [selector, scrollTop] of state.regions) {
      const element = this.contentEl.querySelector<HTMLElement>(selector);
      if (element) {
        element.scrollTop = scrollTop;
      }
    }
  }

  private captureFocusState(): WorkbenchFocusState | null {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !this.contentEl.contains(active)) {
      return null;
    }
    return {
      tagName: active.tagName.toLowerCase(),
      ariaLabel: active.getAttribute("aria-label"),
      segmentId: active.getAttribute("data-segment-id"),
      placeholder: active.getAttribute("placeholder"),
      text:
        active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement
          ? null
          : active.textContent?.trim() || null
    };
  }

  private restoreFocusState(state: WorkbenchFocusState | null): void {
    if (!state) {
      return;
    }
    const candidates = this.contentEl.querySelectorAll<HTMLElement>(state.tagName);
    for (const candidate of candidates) {
      if (
        candidate.getAttribute("aria-label") === state.ariaLabel &&
        candidate.getAttribute("data-segment-id") === state.segmentId &&
        candidate.getAttribute("placeholder") === state.placeholder &&
        (state.text === null || candidate.textContent?.trim() === state.text)
      ) {
        candidate.focus({ preventScroll: true });
        return;
      }
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
    identity.createEl("small", {
      cls: "speech-capture-header__version",
      text: `Speech Capture ${this.plugin.manifest.version}`
    });

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
    const visibleJobs = this.visibleJobs();
    if (visibleJobs.length > 0) {
      for (const job of visibleJobs) {
        const titleText = taskTitle(job.source_display_name);
        const task = aside.createDiv({
          cls: `speech-capture-task-card ${job.job_id === this.selectedJobId ? "is-selected" : ""}`,
          attr: {
            ...(job.job_id === this.selectedJobId
              ? { "aria-current": "page" }
              : {})
          }
        });
        const open = task.createEl("button", {
          cls: "speech-capture-task-card__open",
          attr: {
            type: "button",
            title: titleText,
            "aria-label": `打开任务：${titleText}`
          }
        });
        setTooltip(open, titleText, {
          placement: "right",
          delay: 200,
          classes: ["speech-capture-task-title-tooltip"]
        });
        const row = open.createSpan({ cls: "speech-capture-task-card__title" });
        const wave = row.createSpan({ cls: "speech-capture-task-card__icon" });
        setIcon(wave, "audio-lines");
        row.createEl("strong", {
          text: titleText,
          attr: { title: titleText }
        });
        open.createEl("span", {
          cls: "speech-capture-task-card__status",
          text: this.taskCardStatus(job)
        });
        if (job.recording_date) {
          open.createEl("small", { text: job.recording_date });
        }
        open.addEventListener("click", () => void this.selectJob(job.job_id));
        const manageable = canManageJobData(job.state);
        const manage = task.createEl("button", {
          cls: "speech-capture-task-card__manage",
          text: "管理/删除",
          attr: {
            type: "button",
            title: manageable
              ? "管理记录和空间"
              : "任务处理完成或停止后才可清理数据",
            "aria-label": `管理记录和空间：${titleText}`,
            ...(manageable ? {} : { disabled: "", "aria-disabled": "true" })
          }
        });
        setTooltip(
          manage,
          manageable
            ? "删除原始音频，或删除整条语音记录"
            : "任务处理完成或停止后才可清理数据",
          {
            placement: "right",
            delay: 150
          }
        );
        if (manageable) {
          manage.addEventListener("click", () => {
            this.openRecordManagement(job);
          });
        }
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
    this.renderCurrentTask(main);
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
        text: this.summaryRegenerating
          ? "正在生成候选…"
          : "生成新版笔记候选",
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
    if (snapshot.job.state === "published") {
      const publication = headingActions.createEl("button", {
        text:
          this.summaryRevisions?.can_regenerate || pendingRevision
            ? "查看当前已发布旧版"
            : "查看发布结果",
        attr: { type: "button" }
      });
      publication.addEventListener("click", () => {
        this.taskDetailMode = "publication";
        if (this.publicationState.state === "idle") {
          void this.preparePublication(false, true);
        } else {
          this.render();
        }
      });
    }
    headingActions.createEl("span", {
      cls: `speech-capture-job-state ${
        snapshot.job.state === "published" && this.summaryRevisions?.can_regenerate
          ? "is-warning"
          : "is-good"
      }`,
      text:
        snapshot.job.state === "published"
          ? this.summaryRevisions?.can_regenerate
            ? "已发布 · 修订待更新"
            : "已发布 · 可复核"
          : "处理完成 · 可复核"
    });

    this.renderSummaryRegenerationStatus(main);
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

  private renderSummaryRegenerationStatus(parent: HTMLElement): void {
    const feedback = this.summaryRegenerationFeedback;
    if (!feedback) {
      return;
    }
    const card = parent.createDiv({
      cls: `speech-capture-summary-regeneration is-${feedback.state}`,
      attr: {
        role: feedback.state === "error" ? "alert" : "status",
        "aria-live": "polite"
      }
    });
    const icon = card.createSpan({
      cls: "speech-capture-summary-regeneration__icon"
    });
    setIcon(icon, feedback.state === "working" ? "loader-circle" : "triangle-alert");
    const copy = card.createDiv();
    copy.createEl("strong", {
      text:
        feedback.state === "working"
          ? "正在生成新版笔记候选"
          : "候选笔记尚未生成"
    });
    copy.createEl("p", {
      cls: "speech-capture-summary-regeneration__phase",
      text:
        feedback.state === "working"
          ? this.summaryRegenerationPhase()
          : feedback.message
    });
    if (feedback.state === "working") {
      copy.createEl("p", {
        cls: "speech-capture-summary-regeneration__elapsed",
        text: `已用时 ${this.summaryRegenerationElapsed()}`
      });
      const progress = copy.createDiv({
        cls: "speech-capture-progress is-indeterminate",
        attr: { "aria-label": "正在生成候选笔记" }
      });
      progress.createDiv({ cls: "speech-capture-progress__fill" });
      copy.createEl("small", {
        text: "完成后会自动进入差异页；接受新版并重新发布前，当前 Vault Note 不会变化。"
      });
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
    const audioDeleted = snapshot.job.source_audio_status === "deleted";
    const source = card.createDiv({ cls: "speech-capture-review-audio__source" });
    const sourceCopy = source.createDiv({
      cls: "speech-capture-review-audio__source-copy"
    });
    sourceCopy.createSpan({
      text: offline
        ? "当前无法播放音频，逐字稿仍可阅读和修改"
        : audioDeleted
          ? "原始音频已删除 · 逐字稿、证据和笔记仍可使用"
        : this.localAudioByJobId.has(snapshot.job.job_id)
          ? "当前设备原始音频 · 本地播放"
          : `${workerName} 在线 · 流式播放`
    });
    if (audioDeleted) {
      play.disabled = true;
      slider.disabled = true;
      sourceCopy.createEl("small", {
        text: `已释放 ${formatBytes(snapshot.job.source_audio_deleted_bytes)}，不能再播放或执行依赖音频的处理。`
      });
    } else if (offline) {
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
    const publishedRevision = summaryRevisionIsPublished(
      revision,
      this.publishedSummaryManifest()
    );
    const heading = main.createDiv({ cls: "speech-capture-summary-heading" });
    const copy = heading.createDiv();
    copy.createEl("p", { cls: "speech-capture-eyebrow", text: "NOTE REVISION" });
    copy.createEl("h2", { text: "比较重新生成的笔记" });
    copy.createEl("p", {
      text: "逐字稿修订已用于生成候选笔记；确认前不会替换当前 Note。"
    });
    const headingActions = heading.createDiv({
      cls: "speech-capture-summary-heading__actions"
    });
    const protectedBadge = headingActions.createEl("span", {
      cls: "speech-capture-summary-protected",
      text: "原始证据已保护"
    });
    protectedBadge.prepend(this.choiceMark("shield-check"));
    if (revision.status !== "rejected") {
      const edit = headingActions.createEl("button", {
        cls: "speech-capture-summary-edit-note",
        text: this.summaryDraftOpening
          ? "正在确认版本状态…"
          : this.summaryDraftEditing
          ? "返回差异对照"
          : publishedRevision
            ? `基于已发布 V${revision.candidate_version.toString()} 创建 V${(
                revision.candidate_version + 1
              ).toString()}`
          : revision.status === "accepted"
            ? `编辑 V${revision.candidate_version.toString()} Note`
            : "直接编辑候选 Note",
        attr: { type: "button" }
      });
      edit.disabled = this.summaryDraftOpening;
      edit.addEventListener("click", () => {
        if (this.summaryDraftEditing) {
          this.summaryDraftEditing = false;
          this.render();
          return;
        }
        void this.openSummaryDraftEditor(revision);
      });
    }

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
    if (this.summaryDraftEditing && revision.status !== "rejected") {
      this.renderSummaryDraftEditor(main, revision);
      if (this.isNarrowWorkbench()) {
        this.renderSummaryDecisionPanel(main, revision, true);
      }
      return;
    }
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
      if (change.deletionBasisMissing) {
        const warning = card.createEl("p", {
          cls: "speech-capture-inline-warning",
          text: "机器没有提供这一整块内容的删除依据，请人工核对后再决定是否采用新版。"
        });
        warning.prepend(this.choiceMark("triangle-alert"));
      }
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

  private renderSummaryDraftEditor(
    parent: HTMLElement,
    revision: SummaryRevisionSchema
  ): void {
    const publishedRevision = summaryRevisionIsPublished(
      revision,
      this.publishedSummaryManifest()
    );
    const targetVersion = publishedRevision
      ? revision.candidate_version + 1
      : revision.candidate_version;
    const editor = parent.createDiv({ cls: "speech-capture-summary-draft" });
    editor.createEl("h3", {
      text: publishedRevision
        ? `基于已发布 V${revision.candidate_version.toString()} 创建 V${targetVersion.toString()}`
        : "人工编辑候选 Note"
    });
    editor.createEl("p", {
      text:
        publishedRevision
          ? `保存后会创建新的 V${targetVersion.toString()}，已发布的 V${revision.candidate_version.toString()} 保持只读且不会被覆盖；原始 ASR、校订逐字稿、证据笔记和“我的补充”也不会改变。`
          : revision.status === "accepted"
          ? `保存后会更新待发布的 V${revision.candidate_version.toString()} Note；原始 ASR、校订逐字稿、证据笔记、旧版 Note 和“我的补充”都不会改变。`
          : "这里只修改最终 Note 正文；原始 ASR、校订逐字稿、证据笔记和“我的补充”都不会改变。"
    });
    const textarea = editor.createEl("textarea", {
      cls: "speech-capture-summary-draft__editor",
      attr: {
        "aria-label": "候选 Note Markdown",
        spellcheck: "true"
      }
    });
    textarea.value = this.summaryDraftText;
    textarea.disabled = this.summaryDraftSaving;
    textarea.addEventListener("input", () => {
      this.summaryDraftText = textarea.value;
      this.summaryDraftFeedback = null;
    });
    const footer = editor.createDiv({ cls: "speech-capture-summary-draft__footer" });
    footer.createEl("span", {
      text:
        revision.draft_version > 0
          ? `已保存人工草稿 v${revision.draft_version.toString()}`
          : "尚未保存人工草稿"
    });
    const footerActions = footer.createDiv({
      cls: "speech-capture-summary-draft__actions"
    });
    const save = footerActions.createEl("button", {
      cls: "mod-cta",
      text: this.summaryDraftSaving
        ? "正在保存…"
        : publishedRevision
          ? `保存为 V${targetVersion.toString()}`
        : revision.status === "accepted"
          ? `保存并更新 V${revision.candidate_version.toString()}`
          : "保存人工定稿",
      attr: { type: "button" }
    });
    save.disabled = this.summaryDraftSaving || !this.summaryDraftText.trim();
    save.addEventListener("click", () => void this.saveSummaryDraft(revision));
    if (revision.status === "accepted" && !publishedRevision) {
      const persisted = (
        revision.draft_markdown ?? renderSummaryCandidateMarkdown(revision.after_document)
      ).trim();
      const continuePublication = footerActions.createEl("button", {
        text: `继续发布 V${revision.candidate_version.toString()}`,
        attr: { type: "button" }
      });
      continuePublication.disabled =
        this.summaryDraftSaving || this.summaryDraftText.trim() !== persisted;
      continuePublication.addEventListener("click", () => {
        this.summaryDraftEditing = false;
        this.taskDetailMode = "publication";
        this.allowAutomaticPublicationView = true;
        this.publicationState = { state: "idle" };
        void this.preparePublication(true, true);
      });
    }
    if (this.summaryDraftFeedback) {
      editor.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: this.summaryDraftFeedback
      });
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
    const publishedRevision = summaryRevisionIsPublished(
      revision,
      this.publishedSummaryManifest()
    );
    const panel = parent.createDiv({
      cls: `speech-capture-summary-decision ${inline ? "is-inline" : ""}`
    });
    panel.createEl("h3", { text: "确认这份候选笔记" });
    this.assurance(panel, "sparkles", "只切换机器提炼的 Note 正文");
    this.assurance(panel, "shield-check", "原始 ASR 与证据不会变化");
    this.assurance(panel, "notebook-pen", "“我的补充”保持当前内容");
    if (revision.draft_version > 0) {
      this.assurance(
        panel,
        "file-pen-line",
        `将采用已保存的人工草稿 v${revision.draft_version.toString()}`
      );
    }
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
            ? publishedRevision
              ? `v${revision.candidate_version.toString()} 已发布`
              : `v${revision.candidate_version.toString()} 已成为待发布笔记`
            : `已保留 v${revision.base_version.toString()} 作为当前笔记`
      });
      status.createEl("p", {
        text:
          revision.status === "accepted"
            ? publishedRevision
              ? `当前 Vault 已经使用 V${revision.candidate_version.toString()}；再次编辑会创建下一版本，不会覆盖这份已发布记录。`
              : "Worker 中的新版已经确认；当前 Vault 旧版尚未变化，需要继续完成重新发布。"
            : "此记录只读，当前 Vault Note 与原始证据均保持不变。"
      });
      if (revision.status === "accepted" && !publishedRevision) {
        const publish = status.createEl("button", {
          cls: "mod-cta",
          text: "继续到重新发布",
          attr: { type: "button" }
        });
        publish.addEventListener("click", () => {
          this.taskDetailMode = "publication";
          this.allowAutomaticPublicationView = true;
          if (this.publicationState.state === "idle") {
            void this.preparePublication(false, true);
          } else {
            this.render();
          }
        });
      }
    } else {
      const accept = panel.createEl("button", {
        cls: "mod-cta speech-capture-summary-decision__accept",
        text: this.summaryDecisionSaving
          ? "正在保存…"
          : revision.draft_version > 0
            ? "接受人工定稿"
            : "接受新版笔记",
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
    const replacementVersion = this.publicationReplacementVersion();
    copy.createEl("p", {
      cls: "speech-capture-eyebrow",
      text: replacementVersion === null ? "ACTIVE TASK" : "REPUBLISH NOTE"
    });
    copy.createEl("h2", {
      text:
        replacementVersion === null
          ? taskTitle(job.source_display_name)
          : `重新发布 V${replacementVersion.toString()}`
    });
    heading.createEl("span", {
      cls: `speech-capture-job-state ${replacementVersion === null ? "is-good" : "is-warning"}`,
      text:
        replacementVersion === null
          ? job.state === "published" ? "已发布" : "已处理"
          : this.publicationState.state === "published"
            ? `V${replacementVersion.toString()} 已发布`
            : `V${replacementVersion.toString()} 待发布`
    });
    if (replacementVersion === null) {
      this.renderPublicationRail(main);
    } else {
      this.renderRepublicationRail(main);
    }

    const state = this.publicationState;
    if (state.state === "conflict") {
      switch (state.step) {
        case "notice":
          this.renderPublicationConflictNotice(main, state);
          break;
        case "difference":
          this.renderPublicationConflictDiff(main, state);
          break;
        case "location":
          this.renderPublicationLocation(main, state);
          break;
      }
      return;
    }
    if (state.state === "published") {
      const card = main.createDiv({ cls: "speech-capture-publication-result is-success" });
      card.setAttrs({ role: "status", "aria-live": "polite" });
      setIcon(card.createSpan(), "circle-check-big");
      const result = card.createDiv();
      const version = state.replacement?.publicationVersion;
      result.createEl("h3", {
        text: version === undefined
          ? "已发布到 Obsidian"
          : `V${version.toString()} 已发布到 Obsidian`
      });
      result.createEl("p", { text: "完整产物包已经写入当前 Vault，并通过写入后校验。" });
      const actions = result.createDiv({
        cls: "speech-capture-publication-result__actions"
      });
      const open = actions.createEl("button", {
        cls: "mod-cta",
        text: version === undefined ? "打开 Note" : `打开 V${version.toString()} Note`,
        attr: { type: "button" }
      });
      open.addEventListener("click", () => void this.openPublishedNote(state.targetRelativePath));
      if (state.replacement) {
        const previous = actions.createEl("button", {
          text: `打开 V${Math.max(1, state.replacement.publicationVersion - 1).toString()} Note`,
          attr: { type: "button" }
        });
        previous.addEventListener("click", () =>
          void this.openPublishedNote(state.replacement!.previousTargetRelativePath)
        );
      }
      const review = actions.createEl("button", {
        text: "查看完整逐字稿与证据",
        attr: { type: "button" }
      });
      review.addEventListener("click", () => {
        this.allowAutomaticPublicationView = false;
        this.taskDetailMode = "review";
        this.render();
      });
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
      this.publicationState = { ...state, step: "difference", pathError: undefined };
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
    main.addClass("has-action-dock");
    const body = main.createDiv({ cls: "speech-capture-publication-flow-scroll" });
    const heading = body.createDiv({ cls: "speech-capture-publication-diff-heading" });
    heading.createEl("h3", { text: "已查看发布差异" });
    heading.createEl("p", { text: "当前 Vault 的修改与 Worker 待发布版本都已保留。" });
    const diff = body.createDiv({ cls: "speech-capture-publication-diff" });
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
    const safeguards = body.createDiv({ cls: "speech-capture-publication-safeguards" });
    this.assurance(safeguards, "shield-check", "当前位置不变：保留现有人工与同步修改");
    this.assurance(safeguards, "shield-check", "Worker 版本不变：不做覆盖或逐条合并");
    this.assurance(safeguards, "shield-check", "新位置写入后再次校验完整性");
    const actions = main.createDiv({ cls: "speech-capture-publication-sticky-actions" });
    const back = actions.createEl("button", {
      text: "返回冲突说明",
      attr: { type: "button" }
    });
    back.addEventListener("click", () => {
      this.publicationState = { ...state, step: "notice", pathError: undefined };
      this.render();
    });
    const continueButton = actions.createEl("button", {
      cls: "mod-cta speech-capture-publication-sticky-actions__continue",
      text: "继续：选择保存位置",
      attr: { type: "button" }
    });
    continueButton.addEventListener("click", () => {
      this.publicationState = { ...state, step: "location", pathError: undefined };
      this.render();
    });
  }

  private renderPublicationLocation(
    main: HTMLElement,
    state: Extract<PublicationViewState, { state: "conflict" }>
  ): void {
    const version = this.publicationVersion();
    main.addClass("has-action-dock");
    const body = main.createDiv({ cls: "speech-capture-publication-flow-scroll" });
    const section = body.createDiv({ cls: "speech-capture-publication-location" });
    section.createEl("h3", { text: `选择 V${version.toString()} 保存位置` });
    section.createEl("p", {
      text: `V${Math.max(1, version - 1).toString()} 保持原位且不会被覆盖；V${version.toString()} 将保存为新的独立笔记。`
    });

    const recommended = section.createDiv({
      cls: `speech-capture-publication-destination ${
        state.destinationChoice === "recommended" ? "is-selected" : ""
      }`
    });
    const recommendedRadio = recommended.createEl("input", {
      attr: {
        type: "radio",
        name: "speech-capture-publication-destination",
        value: "recommended",
        "aria-label": "保存到同级新文件夹（推荐）"
      }
    });
    recommendedRadio.checked = state.destinationChoice === "recommended";
    const recommendedCopy = recommended.createDiv();
    const recommendedHeading = recommendedCopy.createDiv({
      cls: "speech-capture-publication-destination__heading"
    });
    recommendedHeading.createEl("strong", { text: "保存到同级新文件夹（推荐）" });
    recommendedHeading.createEl("span", { text: "推荐" });
    recommendedCopy.createEl("small", { text: "新位置" });
    recommendedCopy.createEl("code", { text: `${state.recommendedTargetRelativePath}/note.md` });
    recommended.addEventListener("click", () => {
      if (state.destinationChoice === "recommended") {
        return;
      }
      this.publicationState = {
        ...state,
        destinationChoice: "recommended",
        pathError: undefined
      };
      this.render();
    });

    const custom = section.createDiv({
      cls: `speech-capture-publication-destination ${
        state.destinationChoice === "custom" ? "is-selected" : ""
      }`
    });
    const customRadio = custom.createEl("input", {
      attr: {
        type: "radio",
        name: "speech-capture-publication-destination",
        value: "custom",
        "aria-label": "选择其他新位置"
      }
    });
    customRadio.checked = state.destinationChoice === "custom";
    const customCopy = custom.createDiv();
    const customHeading = customCopy.createDiv({
      cls: "speech-capture-publication-destination__heading"
    });
    customHeading.createEl("strong", { text: "选择其他新位置" });
    const chooseFolder = customHeading.createEl("button", {
      text: "选择文件夹",
      attr: { type: "button" }
    });
    chooseFolder.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      this.openPublicationFolderPicker(state);
    });
    const input = customCopy.createEl("input", {
      attr: {
        type: "text",
        value: state.customTargetRelativePath,
        placeholder: "输入 Vault 内的新文件夹路径；不得覆盖现有 Note",
        "aria-label": `V${version.toString()} 自定义保存位置`
      }
    });
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("input", () => {
      const current = this.publicationState;
      if (current.state !== "conflict") {
        return;
      }
      this.publicationState = {
        ...current,
        destinationChoice: "custom",
        customTargetRelativePath: input.value,
        pathError: undefined
      };
      publish.disabled = this.publicationBusy || !input.value.trim();
    });
    custom.addEventListener("click", () => {
      if (state.destinationChoice === "custom") {
        return;
      }
      this.publicationState = {
        ...state,
        destinationChoice: "custom",
        pathError: undefined
      };
      this.render();
    });

    const safety = section.createDiv({ cls: "speech-capture-publication-location__safety" });
    this.assurance(safety, "shield-check", "原始 ASR、证据和旧版 Note 均不会改变");
    if (state.pathError) {
      section.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: state.pathError,
        attr: { role: "alert" }
      });
    }

    const actions = main.createDiv({ cls: "speech-capture-publication-sticky-actions" });
    const back = actions.createEl("button", {
      text: "返回查看差异",
      attr: { type: "button" }
    });
    back.addEventListener("click", () => {
      this.publicationState = { ...state, step: "difference", pathError: undefined };
      this.render();
    });
    const publishGroup = actions.createDiv({
      cls: "speech-capture-publication-sticky-actions__primary"
    });
    const publish = publishGroup.createEl("button", {
      cls: "mod-cta",
      text: this.publicationBusy
        ? "正在发布并校验…"
        : `发布 V${version.toString()} 到此位置`,
      attr: { type: "button" }
    });
    publish.disabled =
      this.publicationBusy ||
      (state.destinationChoice === "custom" && !state.customTargetRelativePath.trim());
    publish.addEventListener("click", () => {
      const current = this.publicationState;
      if (current.state !== "conflict") {
        return;
      }
      const target =
        current.destinationChoice === "recommended"
          ? current.recommendedTargetRelativePath
          : current.customTargetRelativePath.trim();
      void this.savePublicationToNewLocation(current, target);
    });
    publishGroup.createEl("small", {
      text: `发布后自动校验并打开 V${version.toString()} Note`
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
    const replacementVersion = this.publicationReplacementVersion();
    title.createEl("h2", {
      text: replacementVersion === null ? "发布目标" : "版本与发布"
    });
    title.appendChild(
      this.collapseButton("right", "收起发布目标栏", "panel-right-close")
    );
    if (replacementVersion !== null) {
      this.renderRepublicationSidebar(aside, replacementVersion);
      return;
    }
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

  private renderRepublicationSidebar(aside: HTMLElement, version: number): void {
    const state = this.publicationState;
    const previousVersion = Math.max(1, version - 1);
    const previousTarget =
      state.state === "conflict"
        ? state.status.suggested_target_relative_path
        : state.state === "publishing" || state.state === "published"
          ? state.replacement?.previousTargetRelativePath ?? null
          : null;
    const nextTarget =
      state.state === "conflict"
        ? state.destinationChoice === "custom" && state.customTargetRelativePath.trim()
          ? state.customTargetRelativePath.trim()
          : state.recommendedTargetRelativePath
        : state.state === "publishing" || state.state === "published"
          ? state.targetRelativePath
          : null;

    const previous = aside.createDiv({
      cls: "speech-capture-publication-version-card is-current"
    });
    previous.createEl("h3", { text: `V${previousVersion.toString()} · 当前已发布` });
    previous.createEl("small", { text: "旧位置" });
    previous.createEl("code", { text: previousTarget ?? "正在读取旧版位置…" });
    if (previousTarget) {
      const open = previous.createEl("button", {
        text: `打开 V${previousVersion.toString()}`,
        attr: { type: "button" }
      });
      open.addEventListener("click", () => void this.openPublishedNote(previousTarget));
    }

    const next = aside.createDiv({
      cls: `speech-capture-publication-version-card ${
        state.state === "published" ? "is-published" : "is-pending"
      }`
    });
    next.createEl("h3", {
      text: `V${version.toString()} · ${
        state.state === "published"
          ? "已发布"
          : state.state === "publishing"
            ? "正在发布"
            : "待发布"
      }`
    });
    next.createEl("small", { text: "来源" });
    next.createEl("p", { text: this.publicationRevisionSource(version) });
    next.createEl("small", { text: "新位置" });
    next.createEl("code", { text: nextTarget ?? "尚未选择" });
    this.assurance(next, "circle-check", `不会覆盖 V${previousVersion.toString()}`);

    const checks = aside.createDiv({ cls: "speech-capture-publication-sidebar__safety" });
    checks.createEl("h3", { text: "发布检查清单" });
    this.assurance(
      checks,
      nextTarget ? "circle-check" : "circle",
      nextTarget ? "新位置已选择" : "等待选择新位置"
    );
    this.assurance(
      checks,
      state.state === "conflict" && state.pathError ? "triangle-alert" : "circle-check",
      state.state === "conflict" && state.pathError ? "路径需要调整" : "路径无已知冲突"
    );
    this.assurance(checks, "circle-check", "原始证据已保留");
  }

  private renderRepublicationRail(parent: HTMLElement): void {
    const state = this.publicationState;
    const currentStep =
      state.state === "conflict"
        ? state.step === "notice" || state.step === "difference" ? 0 : 1
        : state.state === "publishing" || state.state === "published"
          ? 2
          : 0;
    const completedThrough = state.state === "published" ? 2 : currentStep - 1;
    const rail = parent.createDiv({ cls: "speech-capture-republication-rail" });
    for (const [index, label] of ["查看差异", "选择保存位置", "发布并校验"].entries()) {
      const item = rail.createDiv({
        cls: `speech-capture-republication-step ${
          index <= completedThrough
            ? "is-complete"
            : index === currentStep
              ? "is-current"
              : ""
        }`
      });
      const mark = item.createSpan({ cls: "speech-capture-republication-step__mark" });
      if (index <= completedThrough) {
        setIcon(mark, "check");
      } else {
        mark.setText((index + 1).toString());
      }
      const copy = item.createDiv();
      copy.createEl("strong", { text: label });
      copy.createEl("small", {
        text:
          index <= completedThrough
            ? "已完成"
            : index === currentStep
              ? "当前步骤"
              : "待进行"
      });
    }
  }

  private publicationReplacementVersion(): number | null {
    const state = this.publicationState;
    if (
      (state.state === "publishing" || state.state === "published") &&
      state.replacement
    ) {
      return state.replacement.publicationVersion;
    }
    const currentVersion = this.summaryRevisions?.current_version ?? 1;
    if (state.state === "conflict" && currentVersion > 1) {
      return currentVersion;
    }
    return null;
  }

  private publicationVersion(): number {
    return this.publicationReplacementVersion() ?? Math.max(
      2,
      this.summaryRevisions?.current_version ?? 1
    );
  }

  private publicationRevisionSource(version: number): string {
    const revision = [...(this.summaryRevisions?.revisions ?? [])]
      .reverse()
      .find(
        (item) =>
          item.status === "accepted" && item.candidate_version === version
      );
    if (!revision) {
      return "已确认的新版 Note";
    }
    return revision.draft_version > 0
      ? `人工定稿 v${revision.draft_version.toString()}`
      : "已确认的机器提炼版本";
  }

  private openPublicationFolderPicker(
    conflict: Extract<PublicationViewState, { state: "conflict" }>
  ): void {
    const modal = new Modal(this.app);
    const version = this.publicationVersion();
    modal.titleEl.setText("选择新位置的父文件夹");
    modal.contentEl.createEl("p", {
      text: `系统会在所选文件夹内创建独立的 V${version.toString()} 任务目录，不会覆盖现有 Note。`
    });
    const select = modal.contentEl.createEl("select", {
      cls: "speech-capture-publication-folder-select",
      attr: { "aria-label": "Vault 文件夹" }
    });
    const folders = this.app.vault
      .getAllLoadedFiles()
      .filter((item): item is TFolder => item instanceof TFolder)
      .map((folder) => folder.path === "/" ? "" : folder.path)
      .filter((path, index, all) => all.indexOf(path) === index)
      .sort((left, right) => left.localeCompare(right, "zh-CN"));
    for (const folder of folders) {
      select.createEl("option", {
        text: folder || "Vault 根目录",
        value: folder
      });
    }
    const recommendedParent = relativeParentPath(conflict.recommendedTargetRelativePath);
    if (folders.includes(recommendedParent)) {
      select.value = recommendedParent;
    }
    const preview = modal.contentEl.createEl("code", {
      cls: "speech-capture-publication-folder-preview"
    });
    const updatePreview = (): string => {
      const parent = select.value;
      const leaf = relativeLeafName(conflict.recommendedTargetRelativePath);
      const target = parent ? `${parent}/${leaf}` : leaf;
      preview.setText(`${target}/note.md`);
      return target;
    };
    updatePreview();
    select.addEventListener("change", updatePreview);
    const actions = modal.contentEl.createDiv({ cls: "speech-capture-confirm-actions" });
    const cancel = actions.createEl("button", {
      text: "取消",
      attr: { type: "button" }
    });
    cancel.addEventListener("click", () => modal.close());
    const choose = actions.createEl("button", {
      cls: "mod-cta",
      text: "使用此位置",
      attr: { type: "button" }
    });
    choose.addEventListener("click", () => {
      const current = this.publicationState;
      if (
        current.state === "conflict" &&
        current.packageData.manifestSha256 === conflict.packageData.manifestSha256
      ) {
        this.publicationState = {
          ...current,
          step: "location",
          destinationChoice: "custom",
          customTargetRelativePath: updatePreview(),
          pathError: undefined
        };
        modal.close();
        this.render();
      }
    });
    modal.open();
    select.focus();
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
    if (this.speakerRenameFeedback) {
      const feedback = editor.createEl("p", {
        cls: `speech-capture-speaker-name-feedback is-${this.speakerRenameFeedback.state}`,
        attr: {
          role: this.speakerRenameFeedback.state === "error" ? "alert" : "status",
          "aria-live": "polite"
        }
      });
      if (
        this.speakerRenameFeedback.state === "saving" ||
        this.speakerRenameFeedback.state === "slow"
      ) {
        const icon = feedback.createSpan({
          cls: "speech-capture-speaker-name-feedback__icon"
        });
        setIcon(icon, "loader-circle");
      }
      feedback.createSpan({ text: this.speakerRenameFeedback.message });
    }
    save.addEventListener("click", () => {
      const current = effectiveSpeakerDisplayName(select.value, this.corrections);
      void this.saveSpeakerDisplayName(
        select.value,
        current.displayName,
        input.value.trim()
      );
    });
  }

  private renderCurrentTask(parent: HTMLElement): void {
    const section = parent.createEl("section", {
      cls: "speech-capture-current-task-inline",
      attr: { "aria-label": "当前任务" }
    });
    const title = section.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "当前任务" });
    const body = section.createDiv({
      cls: "speech-capture-current-task-inline__body"
    });
    const job = this.selectedSnapshot?.job ?? this.selectedJob();
    const facts = body.createDiv({ cls: "speech-capture-task-facts" });
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

    const actions = body.createDiv({ cls: "speech-capture-current-actions" });
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

  private renderIntake(layout: HTMLElement): HTMLElement {
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
    return main;
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

  private renderConfirmation(parent: HTMLElement): void {
    const section = parent.createEl("section", {
      cls: "speech-capture-confirmation-inline",
      attr: { "aria-label": "提交前确认" }
    });
    const title = section.createDiv({ cls: "speech-capture-panel__title" });
    title.createEl("h2", { text: "提交前确认" });

    const body = section.createDiv({
      cls: "speech-capture-confirmation-inline__body"
    });
    const assurances = body.createDiv({ cls: "speech-capture-assurances" });
    this.assurance(assurances, "shield-check", "原始音频不会被修改");
    this.assurance(assurances, "notebook-pen", "补充背景只作为参考");
    this.assurance(assurances, "cloud-upload", "上传完成后可关闭 Obsidian");

    const facts = body.createDiv({ cls: "speech-capture-facts" });
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
      this.renderSubmissionStatus(section);
    }

    const actions = section.createDiv({ cls: "speech-capture-actions" });
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
      this.selectedSnapshot?.job.state === "publishing"
        ? "展开发布目标栏"
        : this.selectedSnapshot?.job.state &&
            isReviewableJobState(this.selectedSnapshot.job.state)
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
    if (snapshot?.job.state === "structuring") {
      const presentation = structuringProgressPresentation(
        snapshot.progress?.stage ?? null,
        snapshot.progress?.stage_progress ?? null,
        snapshot.progress?.detail ?? null
      );
      const stageElapsed = snapshot.progress
        ? formatProcessingElapsed(snapshot.progress.elapsed_seconds)
        : formatElapsedSince(snapshot.job.updated_at);
      const progressAge =
        snapshot.progress?.stage === "structuring"
          ? formatRelativeAge(snapshot.progress.updated_at)
          : null;
      card.createEl("strong", {
        cls: "speech-capture-processing-time",
        text: "正在提炼最终笔记"
      });
      card.createEl("p", {
        text: `当前步骤：${[
          presentation.step,
          presentation.unitText,
          presentation.cacheText,
          presentation.retryText
        ].filter((value): value is string => value !== null).join(" · ")}`
      });
      card.createEl("small", {
        cls: "speech-capture-processing-meta",
        text: `${stageElapsed} · Worker ${this.connectionRecovery ? "连接待恢复" : "已连接"} · ${
          progressAge
            ? `最近保存提炼进度 ${progressAge}`
            : "正在等待第一个提炼进度点"
        }`
      });
      const track = card.createDiv({
        cls: `speech-capture-progress is-large ${presentation.progressPercent === null ? "is-indeterminate" : ""}`,
        attr: {
          role: "progressbar",
          "aria-label": "提炼阶段进度",
          ...(presentation.progressPercent === null
            ? { "aria-valuetext": presentation.step }
            : {
                "aria-valuemin": "0",
                "aria-valuemax": "100",
                "aria-valuenow": presentation.progressPercent.toString(),
                "aria-valuetext": `${presentation.step}，约 ${presentation.progressPercent.toString()}%`
              })
        }
      });
      track.createDiv({
        cls: "speech-capture-progress__fill",
        attr:
          presentation.progressPercent === null
            ? {}
            : { style: `width: ${presentation.progressPercent}%` }
      });
      const safe = card.createDiv({ cls: "speech-capture-processing-safe" });
      const icon = safe.createSpan();
      setIcon(icon, "shield-check");
      safe.createSpan({
        text: `转写、对齐和说话人识别已完成 · ${snapshot.stable_segments.length.toString()} 个稳定片段已安全保存`
      });
      return;
    }
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
            this.selectedSnapshot?.job.state === "publishing"
            ? ".speech-capture-publication-sidebar .speech-capture-collapse-button"
            : this.selectedSnapshot?.job.state &&
                isReviewableJobState(this.selectedSnapshot.job.state)
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
    if (
      this.reviewSaving ||
      this.speakerRenameSaving ||
      this.summaryRegenerating ||
      this.summaryDecisionSaving
    ) {
      return;
    }
    const mode = nextConnectionAttempt(this.connectionRecovery, Date.now());
    if (mode !== null) {
      await this.refreshJobs(mode);
    }
  }

  private async refreshJobs(
    mode: ConnectionAttemptMode = "normal"
  ): Promise<void> {
    if (this.refreshingTasks) {
      if (
        mode === "manual" ||
        (this.selectedJobId !== null &&
          this.selectedSnapshot?.job.job_id !== this.selectedJobId)
      ) {
        this.refreshQueued = true;
      }
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
    const requestedJobId = this.selectedJobId;
    const requestedSelectionEpoch = this.taskSelectionEpoch;
    const requestedMutationEpoch = this.reviewMutationEpoch;
    const previousJobs = this.jobs;
    const hadTaskError = this.taskError !== null;
    const hadConnectionRecovery = this.connectionRecovery !== null;
    const wasReviewingResolved =
      this.selectedSnapshot?.job.state !== undefined &&
      isReviewableJobState(this.selectedSnapshot.job.state);
    const previousRevision = this.selectedSnapshot?.job.revision ?? null;
    const previousCorrectionSequence = this.corrections.at(-1)?.sequence ?? 0;
    const previousSummarySignature = this.summaryRevisionSignature();
    try {
      const jobs = await listJobs(
        new ObsidianWorkerTransport(),
        worker,
        token,
        vaultId
      );
      let selectedSnapshot: JobSnapshotResponse | null = null;
      let corrections: readonly CorrectionSchema[] = [];
      let summaryRevisions: SummaryRevisionListResponse | null = null;
      if (requestedJobId) {
        selectedSnapshot = await getJobSnapshot(
          new ObsidianWorkerTransport(),
          worker,
          token,
          requestedJobId
        );
        const reviewable = isReviewableJobState(selectedSnapshot.job.state);
        corrections =
          reviewable
            ? await listJobCorrections(
                new ObsidianWorkerTransport(),
                worker,
                token,
                requestedJobId
              )
            : [];
        summaryRevisions =
          reviewable
            ? await listJobSummaryRevisions(
                new ObsidianWorkerTransport(),
                worker,
                token,
                requestedJobId
              )
            : null;
      }
      this.jobs = jobs;
      if (
        this.selectedJobId !== requestedJobId ||
        this.taskSelectionEpoch !== requestedSelectionEpoch ||
        this.reviewMutationEpoch !== requestedMutationEpoch
      ) {
        this.refreshQueued = true;
        return;
      }
      this.selectedSnapshot = selectedSnapshot;
      this.corrections = corrections;
      this.summaryRevisions = summaryRevisions;
      if (this.selectedSnapshot) {
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
        sameJobListPresentation(previousJobs, jobs)
      ) {
        return;
      }
      if (
        mode === "normal" &&
        wasReviewingResolved &&
        !hadTaskError &&
        !hadConnectionRecovery &&
        this.selectedSnapshot?.job.state !== undefined &&
        isReviewableJobState(this.selectedSnapshot.job.state) &&
        this.selectedSnapshot.job.revision === previousRevision &&
        (this.corrections.at(-1)?.sequence ?? 0) === previousCorrectionSequence &&
        this.summaryRevisionSignature() === previousSummarySignature
      ) {
        return;
      }
      const selectedState = this.selectedSnapshot?.job.state;
      const hasUnappliedReviewChanges = this.summaryRevisions?.can_regenerate === true;
      if (
        this.publicationState.state === "published" &&
        publishedManifestIsStale(
          this.publicationState.manifestSha256,
          this.summaryRevisions
        )
      ) {
        this.publicationState = { state: "idle" };
        this.publicationBusy = false;
        this.allowAutomaticPublicationView = true;
      }
      const shouldPreparePublication =
        (selectedState === "processed" &&
          this.pendingSummaryRevision() === null &&
          !hasUnappliedReviewChanges) ||
        selectedState === "publishing" ||
        (selectedState === "published" && !hasUnappliedReviewChanges);
      if (
        shouldPreparePublication &&
        this.publicationState.state === "idle" &&
        this.allowAutomaticPublicationView
      ) {
        this.taskDetailMode = "publication";
      }
      this.render();
      if (
        shouldPreparePublication &&
        (this.publicationState.state === "idle" ||
          this.publicationState.state === "waiting_other_client")
      ) {
        const reveal = this.allowAutomaticPublicationView;
        window.setTimeout(() => void this.preparePublication(false, reveal), 0);
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
      if (this.refreshQueued) {
        this.refreshQueued = false;
        window.setTimeout(() => void this.refreshJobs("normal"), 0);
      }
    }
  }

  private async selectJob(jobId: string): Promise<void> {
    this.clearSpeakerRenameSlowTimer();
    this.stopSummaryRegenerationTimer();
    this.taskSelectionEpoch += 1;
    this.selectedJobId = jobId;
    this.selectedSnapshot = null;
    this.corrections = [];
    this.summaryRevisions = null;
    this.selectedSummaryRevisionKey = null;
    this.summaryDecisionSaving = false;
    this.resetSummaryDraftEditor();
    this.summaryRegenerating = false;
    this.summaryRegenerationStartedAt = null;
    this.summaryRegenerationFeedback = null;
    this.taskDetailMode = "review";
    this.allowAutomaticPublicationView =
      this.jobs.find((job) => job.job_id === jobId)?.state !== "published";
    this.publicationState = { state: "idle" };
    this.publicationBusy = false;
    this.selectedSegmentId = null;
    this.speakerFilterId = null;
    this.speakerSearch = "";
    this.speakerRenameSaving = false;
    this.speakerRenameFeedback = null;
    this.taskError = null;
    this.connectionRecovery = null;
    this.viewMode = "task";
    this.render();
    await this.refreshJobs();
  }

  private visibleJobs(): readonly JobSchema[] {
    return this.jobs;
  }

  private openRecordManagement(job: JobSchema): void {
    if (!canManageJobData(job.state)) {
      return;
    }
    const titleText = taskTitle(job.source_display_name);
    const modal = new Modal(this.app);
    modal.titleEl.setText("管理记录和空间");
    modal.modalEl.addClass("speech-capture-record-management");
    const renderMenu = (): void => {
      modal.contentEl.empty();
      modal.contentEl.createEl("p", {
        cls: "speech-capture-record-management__title",
        text: titleText
      });
      modal.contentEl.createEl("p", {
        text: "这里执行的是真实删除，不会只把任务从列表隐藏。"
      });
      const audio = modal.contentEl.createDiv({
        cls: "speech-capture-record-management__option"
      });
      audio.createEl("h3", { text: "仅删除原始音频" });
      audio.createEl("p", {
        text: "永久清理 Worker 上的原始音频、标准化音频和复核播放音频；保留逐字稿、证据、Note 与版本记录。删除后不能再播放音频或重新执行依赖音频的处理。"
      });
      audio.createEl("p", {
        cls: "speech-capture-record-management__size",
        text:
          job.source_audio_status === "deleted"
            ? `原始音频已删除，共释放 ${formatBytes(job.source_audio_deleted_bytes)}。`
            : `原始文件约 ${formatBytes(job.source_size_bytes)}，实际释放空间将在删除后显示。`
      });
      const deleteAudio = audio.createEl("button", {
        text:
          job.source_audio_status === "deleted"
            ? "原始音频已删除"
            : "永久删除原始音频",
        attr: {
          type: "button",
          ...(job.source_audio_status === "deleted" ? { disabled: "" } : {})
        }
      });
      deleteAudio.addEventListener("click", () => {
        renderConfirmation("audio");
      });

      const record = modal.contentEl.createDiv({
        cls: "speech-capture-record-management__option is-danger"
      });
      record.createEl("h3", { text: "删除整条语音记录" });
      record.createEl("p", {
        text: "删除 Worker 上的任务、原始音频、逐字稿、证据、Note 候选和版本记录，并从左侧列表移除。若已发布到当前 Vault，对应发布文件夹会先移入 Obsidian 回收站。"
      });
      const deleteRecord = record.createEl("button", {
        cls: "mod-warning",
        text: "删除整条语音记录",
        attr: { type: "button" }
      });
      deleteRecord.addEventListener("click", () => {
        renderConfirmation("record");
      });
      const close = modal.contentEl.createEl("button", {
        text: "关闭",
        attr: { type: "button" }
      });
      close.addEventListener("click", () => modal.close());
    };
    const renderConfirmation = (kind: "audio" | "record"): void => {
      modal.contentEl.empty();
      const isAudio = kind === "audio";
      modal.contentEl.createEl("h3", {
        text: isAudio ? "永久删除原始音频？" : "删除整条语音记录？"
      });
      modal.contentEl.createEl("p", {
        text: isAudio
          ? "此操作不能撤销。逐字稿、证据和笔记会保留，但音频播放与依赖音频的再次处理将不可用。"
          : "Worker 中的整条记录不能恢复。已发布的 Vault 文件夹会先移入 Obsidian 回收站；回收站是否可恢复取决于你的 Obsidian 设置。"
      });
      const status = modal.contentEl.createDiv({
        cls: "speech-capture-record-management__status",
        attr: { role: "status", "aria-live": "polite" }
      });
      const actions = modal.contentEl.createDiv({
        cls: "speech-capture-confirm-actions"
      });
      const back = actions.createEl("button", {
        text: "返回",
        attr: { type: "button" }
      });
      back.addEventListener("click", renderMenu);
      const confirm = actions.createEl("button", {
        cls: "mod-warning",
        text: isAudio ? "确认永久删除音频" : "确认删除整条记录",
        attr: { type: "button" }
      });
      let completed = false;
      confirm.addEventListener("click", () => {
        if (completed) {
          modal.close();
          return;
        }
        back.disabled = true;
        confirm.disabled = true;
        confirm.setText("正在删除…");
        status.setText(
          isAudio
            ? "正在清理 Worker 音频；笔记资料不会改变。"
            : job.state === "published"
              ? "正在确认已发布笔记的位置并删除记录，请勿关闭此窗口。"
              : "正在删除未发布的 Worker 记录，请勿关闭此窗口。"
        );
        void (isAudio
          ? this.performSourceAudioDeletion(job)
          : this.performFullRecordDeletion(job)
        )
          .then((result) => {
            status.addClass("is-success");
            status.setText(result);
            completed = true;
            confirm.setText("完成");
            confirm.disabled = false;
          })
          .catch((error: unknown) => {
            status.addClass("is-error");
            status.setText(
              error instanceof Error
                ? error.message
                : "删除没有完成；没有把这条记录从列表隐藏。"
            );
            back.disabled = false;
            confirm.disabled = false;
            confirm.setText(isAudio ? "重试删除音频" : "重试删除记录");
          });
      });
    };
    renderMenu();
    modal.open();
  }

  private async performSourceAudioDeletion(job: JobSchema): Promise<string> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (!worker || !token || !canManageJobData(job.state)) {
      throw new Error("Worker 尚未就绪，未删除任何音频。请恢复连接后重试。");
    }
    const current = this.jobs.find((candidate) => candidate.job_id === job.job_id) ?? job;
    const result = await deleteJobSourceAudio(
      new ObsidianWorkerTransport(),
      worker,
      token,
      current
    );
    this.localAudioByJobId.delete(result.job.job_id);
    this.releaseReviewAudioUrl();
    this.jobs = this.jobs.map((candidate) =>
      candidate.job_id === result.job.job_id ? result.job : candidate
    );
    if (this.selectedSnapshot?.job.job_id === result.job.job_id) {
      this.selectedSnapshot = { ...this.selectedSnapshot, job: result.job };
    }
    this.render();
    return result.deleted
      ? `音频已永久删除，共释放 ${formatBytes(result.deleted_bytes)}；逐字稿、证据和笔记仍保留。`
      : `音频此前已经删除，共释放 ${formatBytes(result.deleted_bytes)}；没有重复改动笔记资料。`;
  }

  private async performFullRecordDeletion(job: JobSchema): Promise<string> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (!worker || !token || !canManageJobData(job.state)) {
      throw new Error("Worker 尚未就绪，未删除任何记录。请恢复连接后重试。");
    }
    const current = this.jobs.find((candidate) => candidate.job_id === job.job_id) ?? job;
    let receipt: ReturnType<typeof currentPublicationReceipt> = null;
    if (requiresPublishedFolderCleanup(current.state)) {
      const status = await getPublicationStatus(
        new ObsidianWorkerTransport(),
        worker,
        token,
        current.job_id,
        this.plugin.settings.outputFolder
      ).catch((error: unknown) => {
        throw new Error(
          error instanceof PublicationClientError
            ? `无法确认已发布笔记的位置，未执行删除：${error.message}`
            : "无法确认已发布笔记的位置，未执行删除。"
        );
      });
      receipt = currentPublicationReceipt(status);
    }
    let movedPublishedFolder = false;
    if (receipt) {
      const safePath = safePublishedFolderPath(
        receipt.target_relative_path,
        this.plugin.settings.outputFolder
      );
      if (!safePath) {
        throw new Error("发布目录超出当前语音笔记文件夹，安全检查已停止删除。请检查发布记录。");
      }
      const target = this.app.vault.getAbstractFileByPath(normalizePath(safePath));
      if (target instanceof TFolder) {
        await this.app.fileManager.trashFile(target);
        movedPublishedFolder = true;
      } else if (target instanceof TFile) {
        throw new Error("发布目标不是预期的笔记文件夹，安全检查已停止删除。");
      }
    }
    let result;
    try {
      result = await deleteJob(
        new ObsidianWorkerTransport(),
        worker,
        token,
        current
      );
    } catch (error) {
      if (movedPublishedFolder) {
        throw new Error(
          "已发布笔记文件夹已移入 Obsidian 回收站，但 Worker 记录删除失败。记录仍在左侧列表，可直接重试；需要时可从回收站恢复笔记。"
        );
      }
      throw error;
    }
    const index = this.jobs.findIndex((candidate) => candidate.job_id === current.job_id);
    this.jobs = this.jobs.filter((candidate) => candidate.job_id !== current.job_id);
    if (this.selectedJobId === current.job_id) {
      const next = this.jobs[Math.min(Math.max(index, 0), this.jobs.length - 1)] ?? null;
      if (next) {
        await this.selectJob(next.job_id);
      } else {
        this.openIntake();
      }
    } else {
      this.render();
    }
    const vaultResult = movedPublishedFolder
      ? "已发布笔记文件夹已移入 Obsidian 回收站；"
      : "当前 Vault 没有需要移动的已发布文件夹；";
    return `${vaultResult}Worker 记录已永久删除，共释放 ${formatBytes(result.deleted_bytes)}。`;
  }

  private openIntake(): void {
    this.clearSpeakerRenameSlowTimer();
    this.stopSummaryRegenerationTimer();
    this.releaseReviewAudioUrl();
    this.taskSelectionEpoch += 1;
    this.viewMode = "intake";
    this.selectedJobId = null;
    this.selectedSnapshot = null;
    this.corrections = [];
    this.summaryRevisions = null;
    this.selectedSummaryRevisionKey = null;
    this.summaryDecisionSaving = false;
    this.resetSummaryDraftEditor();
    this.summaryRegenerating = false;
    this.summaryRegenerationStartedAt = null;
    this.summaryRegenerationFeedback = null;
    this.taskDetailMode = "review";
    this.allowAutomaticPublicationView = true;
    this.publicationState = { state: "idle" };
    this.publicationBusy = false;
    this.selectedSegmentId = null;
    this.speakerFilterId = null;
    this.speakerRenameSaving = false;
    this.speakerRenameFeedback = null;
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

  private publishedSummaryManifest(): string | null {
    return this.publicationState.state === "published"
      ? this.publicationState.manifestSha256
      : null;
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
    this.reviewMutationEpoch += 1;
    this.speakerRenameSaving = true;
    this.speakerRenameFeedback = {
      state: "saving",
      message: `正在保存到 ${worker.displayName}，通常几秒内完成。`
    };
    this.taskError = null;
    this.render();
    this.clearSpeakerRenameSlowTimer();
    this.speakerRenameSlowTimer = window.setTimeout(() => {
      if (!this.speakerRenameSaving) {
        return;
      }
      this.speakerRenameFeedback = {
        state: "slow",
        message: `${worker.displayName} 响应较慢，仍在等待；最多等待 12 秒。`
      };
      this.render();
    }, 4_000);
    try {
      const saved = await renameJobSpeakerDisplayName(
        new ObsidianWorkerTransport(),
        worker,
        token,
        { job, speakerId, before, after }
      );
      this.reviewMutationEpoch += 1;
      this.applySpeakerDisplayNameResult(saved);
      this.speakerRenameSaving = false;
      this.speakerRenameFeedback = {
        state: "success",
        message: saved.created ? "显示名已保存。" : "这项显示名修改已经保存过。"
      };
      this.render();
      void this.refreshJobs("manual");
    } catch (error) {
      this.reviewMutationEpoch += 1;
      this.speakerRenameSaving = false;
      this.speakerRenameFeedback = {
        state: "error",
        message:
          error instanceof JobClientError && error.code === "conflict"
            ? "任务内容刚刚发生了变化，正在重新读取；请确认显示名后再保存。"
            : "12 秒内未收到保存确认，正在重新读取 Worker；若显示名没有变化，可以再次保存。"
      };
      this.render();
      void this.refreshJobs("manual");
    } finally {
      this.clearSpeakerRenameSlowTimer();
      this.speakerRenameSaving = false;
      this.render();
    }
  }

  private applySpeakerDisplayNameResult(
    saved: Awaited<ReturnType<typeof renameJobSpeakerDisplayName>>
  ): void {
    this.jobs = this.jobs.map((item) =>
      item.job_id === saved.job.job_id ? saved.job : item
    );
    if (this.selectedSnapshot?.job.job_id === saved.job.job_id) {
      this.selectedSnapshot = { ...this.selectedSnapshot, job: saved.job };
    }
    if (
      !this.corrections.some(
        (correction) => correction.correction_id === saved.correction.correction_id
      )
    ) {
      this.corrections = [...this.corrections, saved.correction].sort(
        (left, right) => left.sequence - right.sequence
      );
    }
  }

  private clearSpeakerRenameSlowTimer(): void {
    if (this.speakerRenameSlowTimer !== null) {
      window.clearTimeout(this.speakerRenameSlowTimer);
      this.speakerRenameSlowTimer = null;
    }
  }

  private async saveSummaryDraft(revision: SummaryRevisionSchema): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.summaryDraftSaving) {
      return;
    }
    const requestEpoch = this.taskSelectionEpoch;
    this.summaryDraftSaving = true;
    this.summaryDraftFeedback = null;
    this.render();
    try {
      const result = await saveJobSummaryRevisionDraft(
        new ObsidianWorkerTransport(),
        worker,
        token,
        {
          job,
          revisionKey: revision.revision_key,
          expectedDraftVersion: revision.draft_version,
          markdown: this.summaryDraftText
        }
      );
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        return;
      }
      this.applySummaryDraftResult(result);
      this.summaryDraftText = result.revision.draft_markdown ?? this.summaryDraftText;
      if (result.revision.status === "accepted" && result.saved) {
        this.publicationState = { state: "idle" };
        this.publicationBusy = false;
        this.allowAutomaticPublicationView = true;
      }
      this.summaryDraftFeedback = result.saved
        ? result.revision.status === "accepted"
          ? `人工定稿 v${result.revision.draft_version.toString()} 已保存；V${result.revision.candidate_version.toString()} 待发布内容已经更新。`
          : `人工草稿 v${result.revision.draft_version.toString()} 已保存；接受新版时将发布这份正文。`
        : "当前内容已经保存。";
    } catch (error) {
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        return;
      }
      this.summaryDraftFeedback =
        error instanceof JobClientError
          ? error.message
          : "人工草稿未能保存，请重新读取后再试。";
      if (error instanceof JobClientError && error.code === "conflict") {
        void this.refreshJobs("manual");
      }
    } finally {
      if (this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.summaryDraftSaving = false;
        this.render();
      }
    }
  }

  private async openSummaryDraftEditor(
    revision: SummaryRevisionSchema
  ): Promise<void> {
    if (this.summaryDraftOpening) {
      return;
    }
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    const requestEpoch = this.taskSelectionEpoch;
    this.summaryDraftOpening = true;
    this.summaryDraftFeedback = null;
    this.render();
    try {
      if (
        revision.status === "accepted" &&
        worker &&
        token &&
        job &&
        this.publicationState.state !== "published"
      ) {
        const status = await getPublicationStatus(
          new ObsidianWorkerTransport(),
          worker,
          token,
          job.job_id,
          this.plugin.settings.outputFolder
        );
        if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
          return;
        }
        const receipt = currentPublicationReceipt(status);
        if (receipt) {
          this.publicationState = {
            state: "published",
            targetRelativePath: receipt.target_relative_path,
            manifestSha256: receipt.manifest_sha256
          };
        }
      }
      this.summaryDraftEditing = true;
      this.summaryDraftText =
        revision.draft_markdown ??
        renderSummaryCandidateMarkdown(revision.after_document);
    } catch (error) {
      this.summaryDraftEditing = true;
      this.summaryDraftText =
        revision.draft_markdown ??
        renderSummaryCandidateMarkdown(revision.after_document);
      this.summaryDraftFeedback =
        error instanceof PublicationClientError
          ? "暂时无法确认当前发布状态；保存时 Worker 仍会保护已发布版本并自动创建下一版。"
          : null;
    } finally {
      if (!job || this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.summaryDraftOpening = false;
        this.render();
      }
    }
  }

  private applySummaryDraftResult(
    result: Awaited<ReturnType<typeof saveJobSummaryRevisionDraft>>
  ): void {
    this.jobs = this.jobs.map((item) =>
      item.job_id === result.job.job_id ? result.job : item
    );
    if (this.selectedSnapshot?.job.job_id === result.job.job_id) {
      this.selectedSnapshot = { ...this.selectedSnapshot, job: result.job };
    }
    if (this.summaryRevisions) {
      this.summaryRevisions = upsertSavedSummaryRevision(
        this.summaryRevisions,
        result.revision
      );
    }
    this.selectedSummaryRevisionKey = result.revision.revision_key;
  }

  private resetSummaryDraftEditor(): void {
    this.summaryDraftOpening = false;
    this.summaryDraftEditing = false;
    this.summaryDraftText = "";
    this.summaryDraftSaving = false;
    this.summaryDraftFeedback = null;
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
    const requestEpoch = this.taskSelectionEpoch;
    this.reviewMutationEpoch += 1;
    this.summaryDecisionSaving = true;
    this.taskError = null;
    this.render();
    try {
      const result = await decideJobSummaryRevision(
        new ObsidianWorkerTransport(),
        worker,
        token,
        {
          job,
          revisionKey: revision.revision_key,
          decision
        }
      );
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.jobs = this.jobs.map((item) =>
          item.job_id === result.job.job_id ? result.job : item
        );
        return;
      }
      this.reviewMutationEpoch += 1;
      this.applySummaryDecisionResult(result);
      this.summaryDecisionSaving = false;
      if (result.revision.artifact_manifest_sha256 !== null) {
        // A decided candidate created a new immutable artifact package.  Never
        // retain the prior publication receipt in view state: doing so would
        // make “打开 Note” reopen the superseded Vault path.
        this.allowAutomaticPublicationView = true;
        this.taskDetailMode = "publication";
        this.publicationState = { state: "idle" };
        this.publicationBusy = false;
      }
      this.render();
      if (result.revision.artifact_manifest_sha256 !== null) {
        window.setTimeout(() => void this.preparePublication(true, true), 0);
      }
      void this.refreshJobs("manual");
    } catch (error) {
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        return;
      }
      this.reviewMutationEpoch += 1;
      this.summaryDecisionSaving = false;
      this.taskError =
        error instanceof JobClientError
          ? error.message
          : "笔记版本未能保存，请重新读取后再试。";
      if (error instanceof JobClientError && error.code === "conflict") {
        void this.refreshJobs("manual");
      } else {
        this.render();
      }
    } finally {
      if (this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.summaryDecisionSaving = false;
        this.render();
      }
    }
  }

  private applySummaryDecisionResult(
    result: Awaited<ReturnType<typeof decideJobSummaryRevision>>
  ): void {
    this.jobs = this.jobs.map((item) =>
      item.job_id === result.job.job_id ? result.job : item
    );
    if (this.selectedSnapshot?.job.job_id === result.job.job_id) {
      this.selectedSnapshot = { ...this.selectedSnapshot, job: result.job };
    }
    const existing = this.summaryRevisions;
    if (!existing) {
      return;
    }
    this.summaryRevisions = {
      ...existing,
      revisions: existing.revisions.map((revision) =>
        revision.revision_key === result.revision.revision_key
          ? result.revision
          : revision
      ),
      current_version:
        result.revision.status === "accepted"
          ? result.revision.candidate_version
          : existing.current_version,
      can_regenerate: false
    };
  }

  private async regenerateSummary(): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.summaryRegenerating) {
      return;
    }
    const requestEpoch = this.taskSelectionEpoch;
    this.reviewMutationEpoch += 1;
    this.summaryRegenerating = true;
    this.summaryRegenerationStartedAt = Date.now();
    this.summaryRegenerationFeedback = {
      state: "working",
      message: "正在生成新版笔记候选。"
    };
    this.startSummaryRegenerationTimer();
    this.taskError = null;
    this.render();
    try {
      const result = await regenerateJobSummary(
        new ObsidianWorkerTransport(),
        worker,
        token,
        job
      );
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.jobs = this.jobs.map((item) =>
          item.job_id === result.job.job_id ? result.job : item
        );
        return;
      }
      this.reviewMutationEpoch += 1;
      this.applySummaryRegenerationResult(result);
      this.selectedSummaryRevisionKey = result.revision.revision_key;
      this.taskDetailMode = "summary";
      this.summaryRegenerating = false;
      this.summaryRegenerationFeedback = null;
      this.stopSummaryRegenerationTimer();
      this.render();
      void this.refreshJobs("manual");
    } catch (error) {
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        return;
      }
      this.reviewMutationEpoch += 1;
      this.summaryRegenerating = false;
      this.summaryRegenerationFeedback = {
        state: "error",
        message:
          error instanceof JobClientError && error.code === "conflict"
            ? "任务刚刚发生了新的修订，已重新读取；请再次生成候选笔记。"
            : "未收到候选笔记完成确认，当前已发布 Note 保持不变；已重新读取 Worker。"
      };
      this.stopSummaryRegenerationTimer();
      this.render();
      void this.refreshJobs("manual");
    } finally {
      if (this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.summaryRegenerating = false;
        this.stopSummaryRegenerationTimer();
      }
    }
  }

  private applySummaryRegenerationResult(
    result: Awaited<ReturnType<typeof regenerateJobSummary>>
  ): void {
    this.jobs = this.jobs.map((item) =>
      item.job_id === result.job.job_id ? result.job : item
    );
    if (this.selectedSnapshot?.job.job_id === result.job.job_id) {
      this.selectedSnapshot = { ...this.selectedSnapshot, job: result.job };
    }
    const existing = this.summaryRevisions;
    const revisions = [
      ...(existing?.revisions ?? []).filter(
        (revision) => revision.revision_key !== result.revision.revision_key
      ),
      result.revision
    ];
    this.summaryRevisions = {
      revisions,
      current_version: existing?.current_version ?? result.revision.base_version,
      manual_section_markdown: existing?.manual_section_markdown ?? "",
      can_regenerate: false
    };
  }

  private startSummaryRegenerationTimer(): void {
    this.stopSummaryRegenerationTimer();
    this.summaryRegenerationTimer = window.setInterval(() => {
      const elapsed = this.contentEl.querySelector<HTMLElement>(
        ".speech-capture-summary-regeneration__elapsed"
      );
      if (elapsed) {
        elapsed.textContent = `已用时 ${this.summaryRegenerationElapsed()}`;
      }
      const phase = this.contentEl.querySelector<HTMLElement>(
        ".speech-capture-summary-regeneration__phase"
      );
      if (phase) {
        phase.textContent = this.summaryRegenerationPhase();
      }
    }, 1_000);
  }

  private stopSummaryRegenerationTimer(): void {
    if (this.summaryRegenerationTimer !== null) {
      window.clearInterval(this.summaryRegenerationTimer);
      this.summaryRegenerationTimer = null;
    }
  }

  private summaryRegenerationElapsed(): string {
    return formatDuration(
      Math.max(0, Date.now() - (this.summaryRegenerationStartedAt ?? Date.now()))
    );
  }

  private summaryRegenerationPhase(): string {
    const elapsed = Math.max(
      0,
      Date.now() - (this.summaryRegenerationStartedAt ?? Date.now())
    );
    if (elapsed < 5_000) {
      return "请求已发送，正在等待 Worker 开始生成。";
    }
    if (elapsed < 30_000) {
      return "Worker 正在生成候选；当前接口不提供可靠百分比。";
    }
    if (elapsed < 3 * 60_000) {
      return "仍在生成候选；已保存的文字与说话人修订会纳入结果。";
    }
    return "仍在处理长录音；页面会持续计时，最多等待 1 小时后明确失败。";
  }

  private async preparePublication(
    force: boolean,
    revealView = true
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.publicationBusy) {
      return;
    }
    if (!force && this.publicationState.state === "conflict") {
      return;
    }
    const requestEpoch = this.taskSelectionEpoch;
    const jobId = job.job_id;
    const keepWaitingView =
      !force && this.publicationState.state === "waiting_other_client";
    this.publicationBusy = true;
    if (revealView) {
      this.taskDetailMode = "publication";
    }
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
      if (!this.isCurrentTaskRequest(jobId, requestEpoch)) {
        return;
      }
      const currentReceipt = currentPublicationReceipt(status);
      if (currentReceipt) {
        const target = currentReceipt.target_relative_path;
        const currentVersion = this.summaryRevisions?.current_version ?? 1;
        const replacement =
          currentVersion > 1 && target !== status.suggested_target_relative_path
            ? {
                previousTargetRelativePath: status.suggested_target_relative_path,
                publicationVersion: currentVersion
              }
            : undefined;
        this.publicationState = {
          state: "published",
          targetRelativePath: target,
          manifestSha256: currentReceipt.manifest_sha256,
          replacement
        };
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
      if (!this.isCurrentTaskRequest(jobId, requestEpoch)) {
        return;
      }
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
      if (!this.isCurrentTaskRequest(jobId, requestEpoch)) {
        return;
      }
      if (inspection.kind === "conflict") {
        const recommendedTargetRelativePath = await chooseNewPublicationPath(
          this.app.vault.adapter,
          status.suggested_target_relative_path,
          this.publicationVersion()
        );
        this.publicationState = {
          state: "conflict",
          status,
          packageData,
          diff: inspection.diff,
          step: "notice",
          recommendedTargetRelativePath,
          destinationChoice: "recommended",
          customTargetRelativePath: ""
        };
        this.publicationBusy = false;
        this.render();
        return;
      }
      await this.publishPreparedPackage(
        status,
        packageData,
        target,
        status.active_lease?.lease_id ?? null,
        requestEpoch
      );
    } catch (error) {
      if (!this.isCurrentTaskRequest(jobId, requestEpoch)) {
        return;
      }
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
    existingLeaseId: string | null,
    requestEpoch: number,
    replacement?: PublicationReplacementContext
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
      const currentRequest = this.isCurrentTaskRequest(
        status.job.job_id,
        requestEpoch
      );
      if (currentRequest) {
        this.publicationState = {
          state: "publishing",
          targetRelativePath,
          replacement
        };
        this.render();
      }
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
      this.jobs = this.jobs.map((item) =>
        item.job_id === acknowledged.job.job_id ? acknowledged.job : item
      );
      if (this.isCurrentTaskRequest(status.job.job_id, requestEpoch)) {
        if (this.selectedSnapshot) {
          this.selectedSnapshot = { ...this.selectedSnapshot, job: acknowledged.job };
        }
        this.publicationState = {
          state: "published",
          targetRelativePath,
          manifestSha256: packageData.manifestSha256,
          replacement
        };
        this.publicationBusy = false;
        this.render();
        if (replacement) {
          window.setTimeout(
            () => void this.openPublishedNote(targetRelativePath, true),
            250
          );
        }
      }
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
          inspection.kind === "conflict" &&
          this.isCurrentTaskRequest(status.job.job_id, requestEpoch)
        ) {
          const recommendedTargetRelativePath = await chooseNewPublicationPath(
            this.app.vault.adapter,
            status.suggested_target_relative_path,
            this.publicationVersion()
          );
          this.publicationState = {
            state: "conflict",
            status,
            packageData,
            diff: inspection.diff,
            step: "notice",
            recommendedTargetRelativePath,
            destinationChoice: "recommended",
            customTargetRelativePath: ""
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
    conflict: Extract<PublicationViewState, { state: "conflict" }>,
    targetRelativePath: string
  ): Promise<void> {
    const worker = this.plugin.preferredWorker();
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    const job = this.selectedSnapshot?.job;
    if (!worker || !token || !job || this.publicationBusy) {
      return;
    }
    const requestEpoch = this.taskSelectionEpoch;
    try {
      if (!targetRelativePath) {
        this.publicationState = {
          ...conflict,
          pathError: "请选择或输入一个新的 Vault 文件夹位置。"
        };
        this.render();
        return;
      }
      if (targetRelativePath === conflict.status.suggested_target_relative_path) {
        const version = this.publicationVersion();
        this.publicationState = {
          ...conflict,
          pathError: `V${version.toString()} 必须保存到与 V${Math.max(1, version - 1).toString()} 不同的新位置。`
        };
        this.render();
        return;
      }
      this.publicationBusy = true;
      this.render();
      const targetInspection = await inspectPublicationTarget(
        this.app.vault.adapter,
        targetRelativePath,
        conflict.packageData
      );
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.publicationBusy = false;
        return;
      }
      if (targetInspection.kind !== "available") {
        this.publicationBusy = false;
        this.publicationState = {
          ...conflict,
          pathError: "所选位置已经存在内容，请选择另一个新位置。"
        };
        this.render();
        return;
      }
      const transport = new ObsidianWorkerTransport();
      const status = await getPublicationStatus(
        transport,
        worker,
        token,
        job.job_id,
        this.plugin.settings.outputFolder
      );
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        this.publicationBusy = false;
        return;
      }
      if (status.manifest_sha256 !== conflict.packageData.manifestSha256) {
        throw new VaultPublicationError("verification", "Worker 发布清单已发生变化，请重新查看。");
      }
      await this.publishPreparedPackage(
        status,
        conflict.packageData,
        targetRelativePath,
        null,
        requestEpoch,
        {
          previousTargetRelativePath: conflict.status.suggested_target_relative_path,
          publicationVersion: this.publicationVersion()
        }
      );
    } catch (error) {
      if (!this.isCurrentTaskRequest(job.job_id, requestEpoch)) {
        return;
      }
      this.publicationBusy = false;
      if (error instanceof VaultPublicationError && error.kind === "unsafe") {
        this.publicationState = {
          ...conflict,
          step: "location",
          pathError: error.message
        };
      } else {
        this.publicationState = {
          state: "error",
          message:
            error instanceof PublicationClientError || error instanceof VaultPublicationError
              ? error.message
              : "新位置未能完成写入，当前内容没有被覆盖。"
        };
      }
      this.render();
    }
  }

  private async openPublishedNote(
    targetRelativePath: string,
    waitForIndex = false
  ): Promise<void> {
    const notePath = normalizePath(`${targetRelativePath}/note.md`);
    let note = this.app.vault.getAbstractFileByPath(notePath);
    if (waitForIndex) {
      for (let attempt = 0; attempt < 20 && !(note instanceof TFile); attempt += 1) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, 100));
        note = this.app.vault.getAbstractFileByPath(notePath);
      }
    }
    if (!(note instanceof TFile)) {
      this.publicationState = {
        state: "error",
        message: "当前发布回执指向的 Note 不存在，请重新检测发布状态。"
      };
      this.render();
      return;
    }
    await this.app.workspace.getLeaf("tab").openFile(note);
  }

  private isCurrentTaskRequest(jobId: string, requestEpoch: number): boolean {
    return isCurrentTaskRequest(
      this.selectedJobId,
      this.taskSelectionEpoch,
      jobId,
      requestEpoch
    );
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

function relativeParentPath(path: string): string {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? "" : path.slice(0, separator);
}

function relativeLeafName(path: string): string {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? path : path.slice(separator + 1);
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

function formatElapsedSince(timestamp: string): string {
  const startedAt = Date.parse(timestamp);
  if (!Number.isFinite(startedAt)) {
    return "提炼已开始";
  }
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1_000));
  if (elapsedSeconds < 60) {
    return "提炼已运行不到 1 分钟";
  }
  const hours = Math.floor(elapsedSeconds / 3_600);
  const minutes = Math.floor((elapsedSeconds % 3_600) / 60);
  return hours > 0
    ? `提炼已运行 ${hours.toString()} 小时 ${minutes.toString()} 分钟`
    : `提炼已运行 ${minutes.toString()} 分钟`;
}

function formatProcessingElapsed(elapsedSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(elapsedSeconds));
  if (safeSeconds < 60) {
    return "提炼已运行不到 1 分钟";
  }
  const hours = Math.floor(safeSeconds / 3_600);
  const minutes = Math.floor((safeSeconds % 3_600) / 60);
  return hours > 0
    ? `提炼已运行 ${hours.toString()} 小时 ${minutes.toString()} 分钟`
    : `提炼已运行 ${minutes.toString()} 分钟`;
}

function formatRelativeAge(timestamp: string): string {
  const updatedAt = Date.parse(timestamp);
  if (!Number.isFinite(updatedAt)) {
    return "时间未知";
  }
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - updatedAt) / 1_000));
  if (elapsedSeconds < 60) {
    return "刚刚";
  }
  const minutes = Math.floor(elapsedSeconds / 60);
  if (minutes < 60) {
    return `${minutes.toString()} 分钟前`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours.toString()} 小时前`;
}

function speakerLabel(speakerId: string | null): string {
  if (!speakerId) {
    return "说话人待识别";
  }
  const suffix = speakerId.match(/(\d+)$/)?.[1];
  return suffix ? `说话人 ${Number(suffix) + 1}` : "说话人";
}
