import { describe, expect, it } from "vitest";

import {
  jobProgressLabel,
  jobStageIndex,
  resourcePresentation,
  taskStatePresentation
} from "../src/task-state-presentation";

const GIB = 1024 ** 3;

describe("task state presentation", () => {
  it("keeps interrupted jobs on their last real processing stage", () => {
    expect(jobStageIndex("waiting_user", "transcribing")).toBe(3);
    expect(jobStageIndex("paused", "aligning")).toBe(4);
    expect(jobStageIndex("failed", "diarizing")).toBe(5);
  });

  it("does not show a live ETA while processing is interrupted", () => {
    expect(jobProgressLabel("paused", 46, 520)).toBe(
      "本阶段停在 46% · 已保存最后进度"
    );
    expect(jobProgressLabel("transcribing", 46, 520)).toBe(
      "本阶段已完成 46% · 预计还需 约 9 分钟"
    );
  });

  it("shows exact disk facts without exposing Worker paths", () => {
    const presentation = resourcePresentation({
      status: "blocked",
      estimated_required_bytes: 12 * GIB,
      disk_reserve_bytes: 20 * GIB,
      disk: { total_bytes: 512 * GIB, free_bytes: 24 * GIB },
      issues: [{ code: "DISK_RESERVE_TOO_LOW" }]
    });

    expect(presentation).toMatchObject({
      kind: "blocked",
      title: "可用空间不足，任务已安全暂停",
      actionLabel: "重新检测空间",
      diskFacts: {
        requiredBytes: 12 * GIB,
        availableBytes: 24 * GIB,
        reserveBytes: 20 * GIB
      }
    });
    expect(JSON.stringify(presentation)).not.toContain("/");
  });

  it("keeps an explicit pause distinct from a failure", () => {
    expect(
      taskStatePresentation({ state: "paused", last_error_code: null }, null)
    ).toMatchObject({ action: "resume", actionLabel: "继续处理" });
    expect(
      taskStatePresentation({ state: "failed", last_error_code: "STAGE_FAILED" }, null)
    ).toMatchObject({ action: "retry", actionLabel: "重试当前阶段" });
  });

  it("routes an unreadable source back to a new task", () => {
    expect(
      taskStatePresentation(
        { state: "waiting_user", last_error_code: "SOURCE_UNDECODABLE" },
        null
      )
    ).toMatchObject({
      action: "new_task",
      actionLabel: "用其他音频新建任务"
    });
  });

  it("lets the resource block own the primary recovery action", () => {
    const report = {
      status: "blocked",
      issues: [{ code: "MEMORY_PRESSURE_BLOCKED" }]
    };
    expect(
      taskStatePresentation(
        { state: "waiting_user", last_error_code: "RESOURCE_PREFLIGHT_BLOCKED" },
        report
      )
    ).toBeNull();
  });
});
