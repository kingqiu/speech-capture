import { apiVersion, Plugin, type WorkspaceLeaf } from "obsidian";

import { ClientUpdateController } from "./client-update";
import {
  ClientUpdateInstallerError,
  launchClientUpdateHelper,
  reconcileClientUpdate,
  stageClientUpdate
} from "./client-update-installer";
import { WorkerCredentialStore } from "./credentials";
import {
  closeObsidianWorkerTransportPool,
  ObsidianWorkerTransport
} from "./obsidian-worker-transport";
import {
  DEFAULT_SETTINGS,
  parseSettings,
  type RemoteWorkerDraftResult,
  type SpeechCaptureSettings,
  type WorkerConnectionSettings
} from "./settings";
import { SpeechCaptureSettingTab } from "./worker-settings-tab";
import { SpeechWorkbenchView, WORKBENCH_VIEW_TYPE } from "./workbench-view";

export default class SpeechCapturePlugin extends Plugin {
  public override settings: SpeechCaptureSettings = DEFAULT_SETTINGS;
  public credentials!: WorkerCredentialStore;
  public clientUpdates!: ClientUpdateController;

  public override async onload(): Promise<void> {
    const storedSettings: unknown = await this.loadData();
    this.settings = parseSettings(storedSettings);
    if (JSON.stringify(storedSettings) !== JSON.stringify(this.settings)) {
      await this.saveData(this.settings);
    }
    this.credentials = new WorkerCredentialStore(this.app);
    this.clientUpdates = new ClientUpdateController(
      new ObsidianWorkerTransport(),
      this.manifest.version,
      apiVersion
    );
    await this.restoreClientUpdateStatus();
    this.addSettingTab(new SpeechCaptureSettingTab(this.app, this));

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
    this.addCommand({
      id: "manage-workers",
      name: "管理处理设备",
      callback: () => this.openWorkerSettings()
    });
  }

  public override onunload(): void {
    closeObsidianWorkerTransportPool();
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
    const worker = this.preferredWorker();
    if (!worker) {
      throw new Error("A preferred Worker is required before authorizing a Vault.");
    }
    this.settings = {
      ...this.settings,
      vaultIdsByWorker: {
        ...this.settings.vaultIdsByWorker,
        [worker.id]: vaultId
      }
    };
    await this.saveData(this.settings);
  }

  public authorizedVaultId(workerId?: string): string | null {
    const id = workerId ?? this.preferredWorker()?.id;
    return id ? this.settings.vaultIdsByWorker[id] ?? null : null;
  }

  public async saveRemoteWorker(
    result: Extract<RemoteWorkerDraftResult, { readonly ok: true }>
  ): Promise<void> {
    this.clientUpdates.reset();
    const worker = result.worker;
    this.settings = {
      ...this.settings,
      workers: [
        ...this.settings.workers.filter(
          (candidate) => candidate.id !== worker.id
        ),
        worker
      ],
      preferredWorkerId: worker.id
    };
    await this.saveData(this.settings);
    await this.notifyWorkerSettingsChanged();
  }

  public async selectWorker(workerId: string): Promise<void> {
    if (!this.settings.workers.some((worker) => worker.id === workerId)) {
      throw new Error("The selected Worker is not configured.");
    }
    this.clientUpdates.reset();
    this.settings = { ...this.settings, preferredWorkerId: workerId };
    await this.saveData(this.settings);
    await this.notifyWorkerSettingsChanged();
  }

  public async removeRemoteWorker(workerId: string): Promise<void> {
    const worker = this.settings.workers.find(
      (candidate) => candidate.id === workerId
    );
    if (!worker || worker.kind !== "remote") {
      return;
    }
    this.clientUpdates.reset();
    this.credentials.clear(workerId);
    const vaultIdsByWorker = { ...this.settings.vaultIdsByWorker };
    delete vaultIdsByWorker[workerId];
    const workers = this.settings.workers.filter(
      (candidate) => candidate.id !== workerId
    );
    this.settings = {
      ...this.settings,
      workers,
      vaultIdsByWorker,
      preferredWorkerId:
        this.settings.preferredWorkerId === workerId
          ? workers.find((candidate) => candidate.kind === "local")?.id ??
            workers[0]?.id ??
            null
          : this.settings.preferredWorkerId
    };
    await this.saveData(this.settings);
    await this.notifyWorkerSettingsChanged();
  }

  public openWorkerSettings(): void {
    const settings = (
      this.app as unknown as {
        readonly setting: {
          open(): void;
          openTabById(pluginId: string): void;
        };
      }
    ).setting;
    settings.open();
    settings.openTabById(this.manifest.id);
  }

  public async startConfirmedClientUpdate(): Promise<void> {
    const candidate = this.clientUpdates.confirmedRelease();
    if (candidate === null) {
      this.clientUpdates.markInstallPreparationFailed(
        "没有可安装的已确认候选包。"
      );
      return;
    }
    this.clientUpdates.markPreparingInstall();
    try {
      const plan = await stageClientUpdate(
        this.app,
        this.manifest.version,
        candidate
      );
      launchClientUpdateHelper(plan);
      this.clientUpdates.markWaitingForExit(
        plan.transactionId,
        plan.targetVersion
      );
    } catch (error) {
      this.clientUpdates.markInstallPreparationFailed(
        error instanceof ClientUpdateInstallerError
          ? error.message
          : "无法准备退出后插件更新。"
      );
    }
  }

  private async activateWorkbench(): Promise<void> {
    const existing = this.app.workspace.getLeavesOfType(WORKBENCH_VIEW_TYPE)[0];
    const leaf = existing ?? this.app.workspace.getLeaf("tab");
    if (!existing) {
      await leaf.setViewState({ type: WORKBENCH_VIEW_TYPE, active: true });
    }
    await this.app.workspace.revealLeaf(leaf);
  }

  private async notifyWorkerSettingsChanged(): Promise<void> {
    await Promise.all(
      this.app.workspace
        .getLeavesOfType(WORKBENCH_VIEW_TYPE)
        .map((leaf) =>
          leaf.view instanceof SpeechWorkbenchView
            ? leaf.view.onWorkerSettingsChanged()
            : Promise.resolve()
        )
    );
  }

  private async restoreClientUpdateStatus(): Promise<void> {
    try {
      const result = await reconcileClientUpdate(this.app, this.manifest.version);
      if (result?.state === "loaded_verified") {
        this.clientUpdates.restoreLoadedVerification(result.previousVersion);
      } else if (result?.state === "rolled_back") {
        this.clientUpdates.restoreRollback(
          result.targetVersion,
          result.errorCode
        );
      } else if (result?.state === "install_failed") {
        this.clientUpdates.restoreInstallFailure(
          result.targetVersion,
          result.errorCode
        );
      }
    } catch {
      // Update diagnostics must never prevent the existing plugin from loading.
    }
  }
}
