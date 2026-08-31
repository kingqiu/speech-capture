export const CONNECTION_RETRY_INTERVAL_MS = 60_000;
export const CONNECTION_RETRY_LIMIT = 3;

export type ConnectionRecovery =
  | {
      readonly state: "retrying";
      readonly attemptsCompleted: number;
      readonly nextAttemptAt: number;
      readonly diagnostic: string | null;
    }
  | { readonly state: "exhausted"; readonly diagnostic: string | null };

export type ConnectionAttemptMode = "normal" | "automatic" | "manual";

export function recoveryAfterFailure(
  current: ConnectionRecovery | null,
  mode: ConnectionAttemptMode,
  now: number,
  diagnostic: string | null = null
): ConnectionRecovery {
  if (mode === "manual") {
    return { state: "exhausted", diagnostic };
  }
  const attemptsCompleted =
    mode === "automatic" && current?.state === "retrying"
      ? current.attemptsCompleted + 1
      : 0;
  return attemptsCompleted >= CONNECTION_RETRY_LIMIT
    ? { state: "exhausted", diagnostic }
    : {
        state: "retrying",
        attemptsCompleted,
        nextAttemptAt: now + CONNECTION_RETRY_INTERVAL_MS,
        diagnostic
      };
}

export function nextConnectionAttempt(
  recovery: ConnectionRecovery | null,
  now: number
): ConnectionAttemptMode | null {
  if (recovery?.state === "exhausted") {
    return null;
  }
  if (recovery?.state === "retrying") {
    return now >= recovery.nextAttemptAt ? "automatic" : null;
  }
  return "normal";
}

export function connectionRecoveryLabel(recovery: ConnectionRecovery): string {
  return recovery.state === "retrying"
    ? "网络波动 · 正在恢复"
    : "暂时无法连接";
}
