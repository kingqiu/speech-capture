import type {
  JobSchema
} from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

export type TaskDetailMode =
  | "review"
  | "summary"
  | "history"
  | "publication";

export type TaskSurface = "active" | "review" | "publication";

export function taskSurface(
  detailMode: TaskDetailMode,
  state: JobSchema["state"] | null
): TaskSurface {
  if (detailMode === "publication" || state === "publishing") {
    return "publication";
  }
  if (state !== null && isReviewableJobState(state)) {
    return "review";
  }
  return "active";
}

export function isReviewableJobState(state: JobSchema["state"]): boolean {
  return state === "processed" || state === "published";
}

export function isCurrentTaskRequest(
  selectedJobId: string | null,
  currentEpoch: number,
  requestJobId: string,
  requestEpoch: number
): boolean {
  return selectedJobId === requestJobId && currentEpoch === requestEpoch;
}
