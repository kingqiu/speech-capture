import { Plugin, type WorkspaceLeaf } from "obsidian";

import { WorkerCredentialStore } from "./credentials";
import {
  DEFAULT_SETTINGS,
  parseSettings,
  type SpeechCaptureSettings,
  type WorkerConnectionSettings
} from "./settings";
import { SpeechWorkbenchView, WORKBENCH_VIEW_TYPE } from "./workbench-view";

export default class SpeechCapturePlugin extends Plugin {
  public override settings: SpeechCaptureSettings = DEFAULT_SETTINGS;
  public credentials!: WorkerCredentialStore;

  public override async onload(): Promise<void> {
    this.settings = parseSettings(await this.loadData());
    this.credentials = new WorkerCredentialStore(this.app);

    this.registerView(
      WORKBENCH_VIEW_TYPE,
      (leaf: WorkspaceLeaf) => new SpeechWorkbenchView(leaf, this)
    );
    this.addRibbonIcon("audio-waveform", "打开语音工作台", () => {
      void this.activateWorkbench();
    });
    this.addCommand({
      id: "open-workbench",
      name: "打开语音工作台",
      callback: () => void this.activateWorkbench()
    });
  }

  public override onunload(): void {
    this.app.workspace.detachLeavesOfType(WORKBENCH_VIEW_TYPE);
  }

  public preferredWorker(): WorkerConnectionSettings | null {
    return (
      this.settings.workers.find(
        (worker) => worker.id === this.settings.preferredWorkerId
      ) ??
      this.settings.workers[0] ??
      null
    );
  }

  public async setSidebarCollapsed(
    side: "left" | "right",
    collapsed: boolean
  ): Promise<void> {
    this.settings = {
      ...this.settings,
      ...(side === "left"
        ? { leftSidebarCollapsed: collapsed }
        : { rightSidebarCollapsed: collapsed })
    };
    await this.saveData(this.settings);
  }

  public async setAuthorizedVaultId(vaultId: string): Promise<void> {
    this.settings = { ...this.settings, vaultId };
    await this.saveData(this.settings);
  }

  private async activateWorkbench(): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(WORKBENCH_VIEW_TYPE)[0];
    const leaf = existing ?? this.app.workspace.getLeaf("tab");
    if (!existing) {
      await leaf.setViewState({ type: WORKBENCH_VIEW_TYPE, active: true });
    }
    await this.app.workspace.revealLeaf(leaf);
  }
}
