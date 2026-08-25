import { describe, expect, it } from "vitest";

import {
  isCurrentTaskRequest,
  taskSurface
} from "../src/task-view-routing";

describe("task view routing", () => {
  it("opens a selected published task in the complete review surface", () => {
    expect(taskSurface("review", "published")).toBe("review");
    expect(taskSurface("review", "processed")).toBe("review");
  });

  it("uses publication only when requested or while publication is active", () => {
    expect(taskSurface("publication", "published")).toBe("publication");
    expect(taskSurface("review", "publishing")).toBe("publication");
    expect(taskSurface("review", "transcribing")).toBe("active");
  });

  it("rejects responses from the previous task selection", () => {
    expect(isCurrentTaskRequest("job-b", 4, "job-a", 3)).toBe(false);
    expect(isCurrentTaskRequest("job-a", 4, "job-a", 3)).toBe(false);
    expect(isCurrentTaskRequest("job-a", 4, "job-a", 4)).toBe(true);
  });
});
