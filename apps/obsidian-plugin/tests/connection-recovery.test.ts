import { describe, expect, it } from "vitest";

import {
  connectionRecoveryLabel,
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
      nextAttemptAt: 1_000 + CONNECTION_RETRY_INTERVAL_MS,
      diagnostic: null
    });
    expect(nextConnectionAttempt(started, 60_999)).toBeNull();
    expect(nextConnectionAttempt(started, 61_000)).toBe("automatic");

    const first = recoveryAfterFailure(started, "automatic", 61_000);
    const second = recoveryAfterFailure(first, "automatic", 121_000);
    const third = recoveryAfterFailure(second, "automatic", 181_000);

    expect(first).toMatchObject({ state: "retrying", attemptsCompleted: 1 });
    expect(second).toMatchObject({ state: "retrying", attemptsCompleted: 2 });
    expect(third).toEqual({ state: "exhausted", diagnostic: null });
    expect(nextConnectionAttempt(third, 999_999)).toBeNull();
  });

  it("keeps the manual entry visible when a manual reconnect fails", () => {
    expect(
      recoveryAfterFailure(
        { state: "exhausted", diagnostic: "ETIMEDOUT" },
        "manual",
        1_000,
        "ECONNRESET"
      )
    ).toEqual({ state: "exhausted", diagnostic: "ECONNRESET" });
  });

  it("keeps a sanitized failure diagnostic for the recovery UI", () => {
    expect(
      recoveryAfterFailure(null, "normal", 1_000, "桌面 HTTPS 网络层返回 ETIMEDOUT")
    ).toMatchObject({
      state: "retrying",
      diagnostic: "桌面 HTTPS 网络层返回 ETIMEDOUT"
    });
  });

  it("distinguishes a recovering network from an exhausted connection", () => {
    expect(
      connectionRecoveryLabel({
        state: "retrying",
        attemptsCompleted: 0,
        nextAttemptAt: 61_000,
        diagnostic: "桌面 HTTPS 网络层返回 ECONNRESET"
      })
    ).toBe("网络波动 · 正在恢复");
    expect(
      connectionRecoveryLabel({ state: "exhausted", diagnostic: "ETIMEDOUT" })
    ).toBe("暂时无法连接");
  });
});
