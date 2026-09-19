import type { App } from "obsidian";

const SECRET_PREFIX = "speech-capture-worker-";

export class WorkerCredentialStore {
  public constructor(private readonly app: App) {}

  public set(workerId: string, token: string): void {
    if (!token) {
      throw new Error("Worker credential cannot be empty.");
    }
    this.app.secretStorage.setSecret(secretId(workerId), token);
  }

  public get(workerId: string): string | null {
    return this.app.secretStorage.getSecret(secretId(workerId)) || null;
  }

  public clear(workerId: string): void {
    this.app.secretStorage.setSecret(secretId(workerId), "");
  }
}

export function secretId(workerId: string): string {
  const encoded = new TextEncoder()
    .encode(workerId)
    .reduce((result, byte) => result + byte.toString(16).padStart(2, "0"), "");
  if (!encoded) {
    throw new Error("Worker ID cannot be empty.");
  }
  return `${SECRET_PREFIX}${encoded}`;
}
