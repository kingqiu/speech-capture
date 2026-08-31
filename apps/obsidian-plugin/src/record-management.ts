import type { JobSchema } from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

const MANAGEABLE_STATES = new Set<JobSchema["state"]>([
  "processed",
  "published",
  "partial",
  "failed",
  "cancelled"
]);

export function canManageJobData(state: JobSchema["state"]): boolean {
  return MANAGEABLE_STATES.has(state);
}

export function requiresPublishedFolderCleanup(
  state: JobSchema["state"]
): boolean {
  return state === "published";
}

export function safePublishedFolderPath(
  targetRelativePath: string,
  outputFolder: string
): string | null {
  const target = safeVaultPath(targetRelativePath);
  const root = safeVaultPath(outputFolder);
  if (!target || !root || target === root || !target.startsWith(`${root}/`)) {
    return null;
  }
  return target;
}

function safeVaultPath(value: string): string | null {
  const normalized = value.trim().replace(/^\/+|\/+$/g, "");
  if (!normalized) {
    return null;
  }
  const parts = normalized.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    return null;
  }
  return parts.join("/");
}
