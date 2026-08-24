import type {
  JobSchema,
  JobState
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

export interface ResourcePresentation {
  readonly kind: "ready" | "warning" | "blocked";
  readonly icon: string;
  readonly shortText: string;
  readonly title: string;
  readonly detail: string;
  readonly actionLabel?: string;
  readonly diskFacts?: {
    readonly requiredBytes: number;
    readonly availableBytes: number;
    readonly reserveBytes: number;
  };
}

export interface TaskStatePresentation {
  readonly kind: "warning" | "blocked";
  readonly icon: string;
  readonly title: string;
  readonly detail: string;
  readonly action?: "resume" | "retry" | "new_task";
  readonly actionLabel?: string;
}

export interface StructuringProgressPresentation {
  readonly step: string;
  readonly progressPercent: number | null;
}

export function jobStageIndex(
  state: JobState,
  lastProgressStage: JobState | null = null
): number {
  const effectiveState =
    ["paused", "waiting_user", "partial", "failed", "cancelled"].includes(state) &&
    lastProgressStage
      ? lastProgressStage
      : state;
  if (effectiveState === "cancelled") {
    return 2;
  }
  switch (effectiveState) {
    case "created":
    case "uploading":
      return 0;
    case "verifying":
      return 1;
    case "queued":
    case "paused":
      return 2;
    case "preprocessing":
    case "transcribing":
      return 3;
    case "aligning":
      return 4;
    case "diarizing":
      return 5;
    case "structuring":
      return 6;
    default:
      return 7;
  }
}

export function jobProgressLabel(
  state: JobState,
  progressPercent: number,
  estimatedRemainingSeconds: number | null
): string {
  if (["paused", "waiting_user", "partial", "failed", "cancelled"].includes(state)) {
    return `本阶段处理未完成（${progressPercent.toString()}%）· 已保存最后进度`;
  }
  return `本阶段已完成 ${progressPercent.toString()}%${estimatedRemainingSeconds === null ? "" : ` · 预计还需 ${formatRemaining(estimatedRemainingSeconds)}`}`;
}

export function structuringProgressPresentation(
  progressStage: JobState | null,
  stageProgress: number | null
): StructuringProgressPresentation {
  if (progressStage !== "structuring" || stageProgress === null) {
    return {
      step: "正在准备提炼上下文",
      progressPercent: null
    };
  }
  const progressPercent = Math.max(0, Math.min(100, Math.round(stageProgress * 100)));
  const step =
    progressPercent < 4
      ? "正在准备提炼上下文"
      : progressPercent < 24
        ? "正在校订完整逐字稿"
        : progressPercent < 30
          ? "正在识别内容类型"
          : progressPercent < 60
            ? "正在提取关键事实与证据"
            : progressPercent < 70
              ? "正在生成笔记初稿"
              : progressPercent < 78
                ? "正在补全说话人与时间线"
                : progressPercent < 88
                  ? "正在核对内容覆盖"
                  : progressPercent < 95
                    ? "正在校验笔记结构与证据"
                    : "正在保存提炼结果";
  return { step, progressPercent };
}

export function taskStatePresentation(
  job: Pick<JobSchema, "state" | "last_error_code">,
  report: Readonly<Record<string, unknown>> | null
): TaskStatePresentation | null {
  if (resourcePresentation(report)?.kind === "blocked") {
    return null;
  }
  if (job.state === "paused") {
    return {
      kind: "warning",
      icon: "pause-circle",
      title: "任务已在安全位置暂停",
      detail: "已上传音频、稳定逐字稿和处理检查点均已保留；继续后会从已保存的阶段恢复。",
      action: "resume",
      actionLabel: "继续处理"
    };
  }
  if (job.state === "waiting_user") {
    if (job.last_error_code === "SOURCE_UNDECODABLE") {
      return {
        kind: "blocked",
        icon: "file-warning",
        title: "无法读取这个音频文件",
        detail: "请选择受支持、可以正常播放的音频文件新建任务；当前任务和上传记录会继续保留。",
        action: "new_task",
        actionLabel: "用其他音频新建任务"
      };
    }
    return {
      kind: "warning",
      icon: "circle-help",
      title: "当前阶段需要处理后继续",
      detail: "已完成阶段和稳定文字均已保留。处理 Worker 上的问题后，可以重新检查并继续。",
      action: "retry",
      actionLabel: "重新检查并继续"
    };
  }
  if (job.state === "partial") {
    return {
      kind: "warning",
      icon: "circle-alert",
      title: "当前任务有未完成的处理区间",
      detail: "已成功生成的内容仍可使用；重试只会继续未完成的当前阶段。",
      action: "retry",
      actionLabel: "重试当前阶段"
    };
  }
  if (job.state === "failed") {
    return {
      kind: "blocked",
      icon: "circle-x",
      title: "当前阶段未能完成",
      detail: "已经完成的阶段、稳定逐字稿和检查点均已保留；重试不会重新上传音频。",
      action: "retry",
      actionLabel: "重试当前阶段"
    };
  }
  if (job.state === "cancelled") {
    return {
      kind: "warning",
      icon: "ban",
      title: "任务已取消",
      detail: "Worker 已停止后续处理。已上传音频、稳定逐字稿和处理检查点仍会保留；这个任务不能恢复。",
      action: "new_task",
      actionLabel: "新建语音任务"
    };
  }
  return null;
}

export function canCancelJob(state: JobState): boolean {
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
    "paused",
    "waiting_user",
    "partial",
    "failed"
  ].includes(state);
}

export function resourcePresentation(
  report: Readonly<Record<string, unknown>> | null
): ResourcePresentation | null {
  if (!report) {
    return null;
  }
  const issues = Array.isArray(report.issues)
    ? report.issues.filter(isUnknownRecord)
    : [];
  const codes = new Set(
    issues.flatMap((issue) =>
      typeof issue.code === "string" ? [issue.code] : []
    )
  );
  if (report.status === "blocked") {
    if (codes.has("DISK_RESERVE_TOO_LOW")) {
      const facts = diskFacts(report);
      return {
        kind: "blocked",
        icon: "hard-drive",
        shortText: "磁盘空间不足 · 已安全暂停",
        title: "可用空间不足，任务已安全暂停",
        detail: "已上传音频、稳定逐字稿和处理检查点都已保留。请先在 Worker 所在 Mac 释放空间，再重新检测。",
        actionLabel: "重新检测空间",
        ...(facts ? { diskFacts: facts } : {})
      };
    }
    return {
      kind: "blocked",
      icon: "circle-alert",
      shortText: "资源不足 · 已安全暂停",
      title: "Worker 资源不足，任务已安全暂停",
      detail: "已经完成的内容不会丢失。请先处理 Worker 上显示的资源问题，再重新检测。",
      actionLabel: "重新检测资源"
    };
  }
  if (report.status === "warning") {
    const memoryWarning =
      codes.has("MEMORY_PRESSURE_WARNING") ||
      codes.has("SWAP_USAGE_WARNING");
    return {
      kind: "warning",
      icon: "circle-alert",
      shortText: memoryWarning ? "内存压力中等 · 处理可能稍慢" : "资源接近安全余量",
      title: memoryWarning ? "内存压力中等，处理可能稍慢" : "Worker 资源接近安全余量",
      detail: "不会影响已经保存的内容；Worker 会继续在安全边界内处理。"
    };
  }
  return {
    kind: "ready",
    icon: "circle-check",
    shortText: "资源状态正常",
    title: "资源状态正常",
    detail: ""
  };
}

function diskFacts(
  report: Readonly<Record<string, unknown>>
): ResourcePresentation["diskFacts"] {
  const disk = isUnknownRecord(report.disk) ? report.disk : null;
  const requiredBytes = finiteNonNegative(report.estimated_required_bytes);
  const availableBytes = finiteNonNegative(disk?.free_bytes);
  const reserveBytes = finiteNonNegative(report.disk_reserve_bytes);
  return requiredBytes === null || availableBytes === null || reserveBytes === null
    ? undefined
    : { requiredBytes, availableBytes, reserveBytes };
}

function finiteNonNegative(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function formatRemaining(seconds: number): string {
  if (seconds < 60) {
    return "不到 1 分钟";
  }
  return `约 ${Math.ceil(seconds / 60).toString()} 分钟`;
}

function isUnknownRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
