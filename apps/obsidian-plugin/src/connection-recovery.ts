export const CONNECTION_RETRY_INTERVAL_MS = 60_000;
export const CONNECTION_RETRY_LIMIT = 3;

export type ConnectionRecovery =
  | {
      readonly state: "retrying";
      readonly attemptsCompleted: number;
      readonly nextAttemptAt: number;
    }
  | { readonly state: "exhausted" };

export type ConnectionAttemptMode = "normal" | "automatic" | "manual";

export function recoveryAfterFailure(
  current: ConnectionRecovery | null,
  mode: ConnectionAttemptMode,
  now: number
): ConnectionRecovery {
  if (mode === "manual") {
    return { state: "exhausted" };
  }
  const attemptsCompleted =
    mode === "automatic" && current?.state === "retrying"
      ? current.attemptsCompleted + 1
      : 0;
  return attemptsCompleted >= CONNECTION_RETRY_LIMIT
    ? { state: "exhausted" }
    : {
        state: "retrying",
        attemptsCompleted,
        nextAttemptAt: now + CONNECTION_RETRY_INTERVAL_MS
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
