import { describe, expect, it } from "vitest";

import {
  CONNECTION_RETRY_INTERVAL_MS,
  nextConnectionAttempt,
  recoveryAfterFailure
} from "../src/connection-recovery";

describe("connection recovery policy", () => {
  it("waits one minute and stops after three automatic failures", () => {
    const started = recoveryAfterFailure(null, "normal", 1_000);
    expect(started).toEqual({
      state: "retrying",
      attemptsCompleted: 0,
      nextAttemptAt: 1_000 + CONNECTION_RETRY_INTERVAL_MS
    });
    expect(nextConnectionAttempt(started, 60_999)).toBeNull();
    expect(nextConnectionAttempt(started, 61_000)).toBe("automatic");

    const first = recoveryAfterFailure(started, "automatic", 61_000);
    const second = recoveryAfterFailure(first, "automatic", 121_000);
    const third = recoveryAfterFailure(second, "automatic", 181_000);

    expect(first).toMatchObject({ state: "retrying", attemptsCompleted: 1 });
    expect(second).toMatchObject({ state: "retrying", attemptsCompleted: 2 });
    expect(third).toEqual({ state: "exhausted" });
    expect(nextConnectionAttempt(third, 999_999)).toBeNull();
  });

  it("keeps the manual entry visible when a manual reconnect fails", () => {
    expect(
      recoveryAfterFailure({ state: "exhausted" }, "manual", 1_000)
    ).toEqual({ state: "exhausted" });
  });
});
