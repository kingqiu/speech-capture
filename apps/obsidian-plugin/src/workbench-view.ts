import { ItemView, setIcon, type WorkspaceLeaf } from "obsidian";

import type {
  JobSchema,
  JobSnapshotResponse
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
import type SpeechCapturePlugin from "./main";
import {
  applyJobAction,
  getJobSnapshot,
  JobClientError,
  listJobs
} from "./job-client";
import { ObsidianWorkerTransport } from "./obsidian-worker-transport";
import {
  SubmissionError,
  submitRecording,
  type SubmissionProgress
} from "./upload-client";
import {
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

export class SpeechWorkbenchView extends ItemView {
  private viewMode: "intake" | "pairing" | "task" = "intake";
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
      this.workbenchWidth = entry.contentRect.width;
      this.applyWorkbenchLayoutSize();
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
      this.renderActiveTask(layout);
      this.renderCurrentTask(layout);
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
        task.createEl("span", { text: jobStateLabel(job.state) });
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
        text: this.taskError
      });
    }
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
      cls: "speech-capture-drop-zone",
      attr: { tabindex: "0" }
    });
    const input = dropZone.createEl("input", {
      cls: "speech-capture-visually-hidden",
      attr: { type: "file", accept: "audio/*" }
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
      attr: { type: "date", value: this.draft.recordingDate }
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
        rows: "4"
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
        attr: { type: "button" }
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
      warning.createSpan({ text: "未检测到可用 Worker。任务和草稿不会切换到其他设备。" });
      const retry = warning.createEl("button", {
        text: "重新检测",
        attr: { type: "button" }
      });
      retry.addEventListener("click", () => void this.refreshWorker());
    } else if (this.workerProbe?.state === "pairing_required") {
      const warning = field.createDiv({ cls: "speech-capture-inline-warning" });
      warning.createSpan({ text: "需要连接此设备。配对完成前不会上传音频。" });
      const connect = warning.createEl("button", {
        text: "开始连接",
        attr: { type: "button" }
      });
      connect.addEventListener("click", () => this.openPairing());
    } else if (this.workerProbe?.state === "incompatible") {
      field.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: "当前版本无法与此 Worker 一起使用。"
      });
    } else if (this.workerProbe?.state === "blocked") {
      field.createEl("p", {
        cls: "speech-capture-inline-warning",
        text: "Worker 已连接，但资源或模型尚未准备好。"
      });
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
      "处理临时文件",
      estimate ? `约 ${formatBytes(estimate.workingBytes)}` : "验证音频后确认"
    );
    this.fact(
      facts,
      "预计总占用",
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
    const right = layout.createEl("button", {
      cls: "speech-capture-restore-handle is-right",
      attr: { type: "button", "aria-label": "展开提交确认栏" }
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
      attr: { type: "button" }
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
        : "任务已进入队列，等待 Worker 更新进度"
    });
    const track = card.createDiv({ cls: "speech-capture-progress is-large" });
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
          ? ".speech-capture-current-task .speech-capture-collapse-button"
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
    const vaultId = this.plugin.settings.vaultId;
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
      }
      this.taskError = null;
      this.connectionRecovery = null;
      this.render();
    } catch (error) {
      if (error instanceof JobClientError && error.code === "unavailable") {
        this.connectionRecovery = recoveryAfterFailure(
          this.connectionRecovery,
          mode,
          Date.now()
        );
        if (this.viewMode === "task") {
          this.render();
        }
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
    this.taskError = null;
    this.connectionRecovery = null;
    this.viewMode = "task";
    this.render();
    await this.refreshJobs();
  }

  private openIntake(): void {
    this.viewMode = "intake";
    this.selectedJobId = null;
    this.selectedSnapshot = null;
    this.taskError = null;
    this.connectionRecovery = null;
    this.render();
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
    const vaultId = this.plugin.settings.vaultId;
    const token = worker ? this.plugin.credentials.get(worker.id) : null;
    if (!worker || !file || !vaultId || !token || !this.selectedProfileCanStart()) {
      this.submissionState = {
        state: "error",
        message: "提交条件已变化，请重新检测 Worker 后再试"
      };
      this.render();
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
    }
  }

  private renderSubmissionStatus(parent: HTMLElement): void {
    const status = parent.createDiv({
      cls: `speech-capture-submission is-${this.submissionState.state}`,
      attr: { role: "status" }
    });
    if (this.submissionState.state === "running") {
      const progress = this.submissionState.progress;
      const percent = Math.round(
        (progress.processedBytes / Math.max(1, progress.totalBytes)) * 100
      );
      const row = status.createDiv({ cls: "speech-capture-submission__row" });
      row.createEl("strong", { text: submissionPhaseLabel(progress.phase) });
      row.createSpan({ text: `${percent}%` });
      const track = status.createDiv({ cls: "speech-capture-progress" });
      track.createDiv({
        cls: "speech-capture-progress__fill",
        attr: { style: `width: ${percent}%` }
      });
      status.createEl("p", {
        text:
          progress.phase === "uploading"
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
        return { text: `${workerName} · 已连接`, className: "is-warning" };
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
