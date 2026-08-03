export const SETTINGS_SCHEMA_VERSION = 1 as const;

export interface WorkerConnectionSettings {
  readonly id: string;
  readonly displayName: string;
  readonly endpoint: string;
  readonly kind: "local" | "remote";
}

export interface SpeechCaptureSettings {
  readonly schemaVersion: typeof SETTINGS_SCHEMA_VERSION;
  readonly vaultId: string | null;
  readonly workers: readonly WorkerConnectionSettings[];
  readonly preferredWorkerId: string | null;
  readonly preferredProfile: "accuracy" | "speed";
  readonly outputFolder: string;
  readonly leftSidebarCollapsed: boolean;
  readonly rightSidebarCollapsed: boolean;
}

export const LOCAL_WORKER_ID = "local-worker";

const LOCAL_WORKER: WorkerConnectionSettings = Object.freeze({
  id: LOCAL_WORKER_ID,
  displayName: "这台 Mac",
  endpoint: "http://127.0.0.1:8765",
  kind: "local"
});

export const DEFAULT_SETTINGS: SpeechCaptureSettings = Object.freeze({
  schemaVersion: SETTINGS_SCHEMA_VERSION,
  vaultId: null,
  workers: Object.freeze([LOCAL_WORKER]),
  preferredWorkerId: LOCAL_WORKER_ID,
  preferredProfile: "accuracy",
  outputFolder: "Speech Notes",
  leftSidebarCollapsed: false,
  rightSidebarCollapsed: false
});

export function parseSettings(value: unknown): SpeechCaptureSettings {
  if (!isRecord(value)) {
    return DEFAULT_SETTINGS;
  }
  const parsedWorkers = Array.isArray(value.workers)
    ? value.workers.flatMap((worker) => parseWorker(worker))
    : [];
  const workers = withLocalCandidate(parsedWorkers);
  const preferredWorkerId =
    typeof value.preferredWorkerId === "string" &&
    workers.some((worker) => worker.id === value.preferredWorkerId)
      ? value.preferredWorkerId
      : null;
  return {
    schemaVersion: SETTINGS_SCHEMA_VERSION,
    vaultId:
      typeof value.vaultId === "string" && isSafeIdentifier(value.vaultId)
        ? value.vaultId
        : null,
    workers,
    preferredWorkerId,
    preferredProfile: value.preferredProfile === "speed" ? "speed" : "accuracy",
    outputFolder:
      typeof value.outputFolder === "string" && value.outputFolder.trim()
        ? value.outputFolder.trim()
        : DEFAULT_SETTINGS.outputFolder,
    leftSidebarCollapsed: value.leftSidebarCollapsed === true,
    rightSidebarCollapsed: value.rightSidebarCollapsed === true
  };
}

function parseWorker(value: unknown): WorkerConnectionSettings[] {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    !value.id ||
    typeof value.displayName !== "string" ||
    !value.displayName ||
    typeof value.endpoint !== "string" ||
    (value.kind !== "local" && value.kind !== "remote")
  ) {
    return [];
  }
  const endpoint = parseEndpoint(value.endpoint, value.kind);
  if (endpoint === null) {
    return [];
  }
  return [
    {
      id: value.id,
      displayName: value.displayName,
      endpoint,
      kind: value.kind
    }
  ];
}

function parseEndpoint(
  endpoint: string,
  kind: "local" | "remote"
): string | null {
  try {
    const url = new URL(endpoint);
    if (url.username || url.password || url.search || url.hash) {
      return null;
    }
    const loopbackHttp =
      url.protocol === "http:" &&
      (url.hostname === "127.0.0.1" || url.hostname === "localhost");
    if (kind === "local" ? !loopbackHttp : url.protocol !== "https:") {
      return null;
    }
    return normalizeEndpoint(url);
  } catch {
    return null;
  }
}

function normalizeEndpoint(url: URL): string {
  url.pathname = url.pathname.replace(/\/+$/, "");
  return url.toString().replace(/\/$/, "");
}

function withLocalCandidate(
  workers: readonly WorkerConnectionSettings[]
): WorkerConnectionSettings[] {
  return workers.some((worker) => worker.kind === "local")
    ? [...workers]
    : [...workers, LOCAL_WORKER];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSafeIdentifier(value: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
}
