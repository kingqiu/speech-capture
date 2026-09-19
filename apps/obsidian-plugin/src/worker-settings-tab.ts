import { App, Notice, PluginSettingTab, Setting } from "obsidian";

import type SpeechCapturePlugin from "./main";
import { ObsidianWorkerTransport } from "./obsidian-worker-transport";
import { remoteWorkerFromDraft } from "./settings";
import { probeWorker } from "./worker-probe";

export class SpeechCaptureSettingTab extends PluginSettingTab {
  public constructor(
    app: App,
    private readonly speechCapturePlugin: SpeechCapturePlugin
  ) {
    super(app, speechCapturePlugin);
  }

  public override display(): void {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "语音处理设备" });
    containerEl.createEl("p", {
      text: "选择每次新建语音任务默认使用的 Mac。远程连接必须使用私有 HTTPS 网络。"
    });

    const remoteWorkers = this.speechCapturePlugin.settings.workers.filter(
      (worker) => worker.kind === "remote"
    );
    if (remoteWorkers.length === 0) {
      containerEl.createEl("p", {
        text: "尚未连接家中 Mac。当前只会检测这台 Mac 上的 Worker。"
      });
    }
    for (const worker of remoteWorkers) {
      const isPreferred =
        this.speechCapturePlugin.settings.preferredWorkerId === worker.id;
      const hasCredential = this.speechCapturePlugin.credentials.get(worker.id) !== null;
      new Setting(containerEl)
        .setName(worker.displayName)
        .setDesc(
          `私有 HTTPS · ${hasCredential ? "授权已保存在系统钥匙串" : "等待配对"}${isPreferred ? " · 默认设备" : ""}`
        )
        .addButton((button) => {
          button
            .setButtonText(isPreferred ? "当前默认" : "设为默认")
            .setDisabled(isPreferred)
            .onClick(async () => {
              await this.speechCapturePlugin.selectWorker(worker.id);
              this.display();
            });
        })
        .addButton((button) => {
          button.setButtonText("从本设备移除").onClick(async () => {
            await this.speechCapturePlugin.removeRemoteWorker(worker.id);
            new Notice("已从这台电脑移除连接信息；家中 Mac 上的任务不会被删除");
            this.display();
          });
        });
    }

    const localWorker = this.speechCapturePlugin.settings.workers.find(
      (worker) => worker.kind === "local"
    );
    if (localWorker) {
      const isPreferred =
        this.speechCapturePlugin.settings.preferredWorkerId === localWorker.id;
      new Setting(containerEl)
        .setName("这台 Mac")
        .setDesc("只检测本机 Worker；不可用时不会静默切换设备")
        .addButton((button) => {
          button
            .setButtonText(isPreferred ? "当前默认" : "设为默认")
            .setDisabled(isPreferred)
            .onClick(async () => {
              await this.speechCapturePlugin.selectWorker(localWorker.id);
              this.display();
            });
        });
    }

    this.renderClientUpdateSettings(containerEl);

    const details = containerEl.createEl("details");
    details.createEl("summary", { text: "连接新的家中 Mac" });
    details.createEl("p", {
      text: "请先让两台电脑接入同一个私有网络，并在家中 Mac 上启用 HTTPS 连接。这里不会保存密码或配对码。"
    });
    let displayName = "书房 Mac";
    let endpoint = "";
    const error = details.createEl("p", {
      cls: "setting-item-description",
      attr: { role: "alert" }
    });
    new Setting(details)
      .setName("设备名称")
      .setDesc("仅用于在语音工作台中识别这台设备")
      .addText((text) => {
        text.setPlaceholder("例如：书房 Mac").setValue(displayName).onChange((value) => {
          displayName = value;
          error.setText("");
        });
      });
    new Setting(details)
      .setName("安全连接地址")
      .setDesc("只接受以 https:// 开头、且不包含用户名、密码或参数的私有地址")
      .addText((text) => {
        text.setPlaceholder("https://家中设备的私有地址").onChange((value) => {
          endpoint = value;
          error.setText("");
        });
      });
    new Setting(details)
      .setName("连接检查")
      .setDesc("保存前会检查 HTTPS、版本和必要能力；不会上传音频")
      .addButton((button) => {
        button.setButtonText("检测并保存").setCta().onClick(async () => {
          const result = remoteWorkerFromDraft(displayName, endpoint);
          if (!result.ok) {
            error.setText(remoteWorkerDraftError(result.reason));
            return;
          }
          button.setDisabled(true).setButtonText("正在检测…");
          const probe = await probeWorker(
            new ObsidianWorkerTransport(),
            result.worker,
            null
          );
          if (probe.state === "unreachable") {
            error.setText(
              `无法连接：${probe.diagnostic}。请确认私有网络在线、地址正确，并且家中 Worker 已启动。`
            );
            button.setDisabled(false).setButtonText("检测并保存");
            return;
          }
          if (probe.state === "incompatible") {
            error.setText("这个 Worker 版本与当前插件不兼容，请先更新家中 Worker。");
            button.setDisabled(false).setButtonText("检测并保存");
            return;
          }
          await this.speechCapturePlugin.saveRemoteWorker(result);
          new Notice("已保存家中 Mac。请回到语音工作台完成一次配对");
          this.display();
        });
      });
  }

  private renderClientUpdateSettings(containerEl: HTMLElement): void {
    containerEl.createEl("h2", { text: "插件更新" });
    containerEl.createEl("p", {
      text: "检查更新、下载校验后，只需确认一次，再完全退出 Obsidian。助手会保留设置、备份旧版并安装，随后自动重开当前笔记库；无需切换到终端。"
    });
    const worker = this.speechCapturePlugin.preferredWorker();
    const token = worker
      ? this.speechCapturePlugin.credentials.get(worker.id)
      : null;
    const state = this.speechCapturePlugin.clientUpdates.state;
    const status = new Setting(containerEl)
      .setName(clientUpdateName(state.phase))
      .setDesc(clientUpdateDescription(state));

    if (state.phase === "available") {
      status.addButton((button) => {
        button.setButtonText("下载并校验").setCta().onClick(async () => {
          if (!worker || !token) {
            new Notice("请先完成当前 Worker 的配对");
            return;
          }
          const pending = this.speechCapturePlugin.clientUpdates.downloadAndVerify(
            worker,
            token
          );
          this.display();
          await pending;
          this.display();
        });
      });
      status.addButton((button) => {
        button.setButtonText("重新检查").onClick(() => {
          void this.checkClientUpdate(worker, token);
        });
      });
      return;
    }
    if (state.phase === "awaiting_confirmation" || state.phase === "confirmed") {
      status.addButton((button) => {
        button.setButtonText("确认更新，退出后安装").setWarning().onClick(async () => {
          if (this.speechCapturePlugin.clientUpdates.state.phase === "awaiting_confirmation") {
            this.speechCapturePlugin.clientUpdates.confirmPreparedUpdate();
          }
          const pending = this.speechCapturePlugin.startConfirmedClientUpdate();
          this.display();
          await pending;
          this.display();
          if (this.speechCapturePlugin.clientUpdates.state.phase === "waiting_for_exit") {
            new Notice("更新助手已准备。请按 Command + Q 完全退出 Obsidian；安装后会重新打开当前 Vault。", 10_000);
          }
        });
      });
      status.addButton((button) => {
        button.setButtonText("取消").onClick(() => {
          this.speechCapturePlugin.clientUpdates.reset();
          this.display();
        });
      });
      return;
    }
    if (
      state.phase === "checking" ||
      state.phase === "downloading" ||
      state.phase === "preparing_install" ||
      state.phase === "waiting_for_exit"
    ) {
      status.addButton((button) => {
        button
          .setButtonText(
            state.phase === "checking"
              ? "正在检查…"
              : state.phase === "downloading"
                ? "正在校验…"
                : state.phase === "preparing_install"
                  ? "正在准备安装事务…"
                  : "等待完全退出…"
          )
          .setDisabled(true);
      });
      return;
    }
    status.addButton((button) => {
      button
        .setButtonText("检查更新")
        .setDisabled(!worker || !token)
        .onClick(() => {
          void this.checkClientUpdate(worker, token);
        });
    });
  }

  private async checkClientUpdate(
    worker: ReturnType<SpeechCapturePlugin["preferredWorker"]>,
    token: string | null
  ): Promise<void> {
    if (!worker || !token) {
      new Notice("请先完成当前 Worker 的配对");
      return;
    }
    const pending = this.speechCapturePlugin.clientUpdates.check(worker, token);
    this.display();
    await pending;
    this.display();
  }
}

function clientUpdateName(
  phase: SpeechCapturePlugin["clientUpdates"]["state"]["phase"]
): string {
  switch (phase) {
    case "idle":
      return "尚未检查";
    case "checking":
      return "正在检查候选版本";
    case "current":
      return "当前已是最新版本";
    case "incompatible":
      return "发现候选版本，但 Obsidian 版本不兼容";
    case "available":
      return "发现可下载的候选版本";
    case "downloading":
      return "正在下载并校验候选包";
    case "awaiting_confirmation":
      return "候选包校验通过，等待明确确认";
    case "confirmed":
      return "候选包已确认，但尚未安装";
    case "preparing_install":
      return "正在准备退出后安装事务";
    case "waiting_for_exit":
      return "更新助手正在等待 Obsidian 完全退出";
    case "loaded_verified":
      return "更新完成，已确认实际加载版本";
    case "rolled_back":
      return "更新失败，旧版本已恢复";
    case "failed":
      return "更新检查未完成";
  }
}

function clientUpdateDescription(
  state: SpeechCapturePlugin["clientUpdates"]["state"]
): string {
  switch (state.phase) {
    case "idle":
      return "检查操作不会上传音频、逐字稿或笔记。";
    case "checking":
      return "正在通过当前 Worker 的已授权只读接口检查。";
    case "current":
      return `当前 ${state.currentVersion}，Worker 最新版本也是 ${state.latestVersion}。`;
    case "incompatible":
      return `候选 ${state.latestVersion} 需要 Obsidian ${state.minAppVersion} 或更高；当前应用版本为 ${state.appVersion}。`;
    case "available":
      return `当前 ${state.currentVersion}，候选 ${state.release.version}；下载前不会修改任何本地插件文件。`;
    case "downloading":
      return `正在验证 ${state.release.version} 的大小、SHA-256、ZIP 白名单和插件身份。`;
    case "awaiting_confirmation":
      return `版本 ${state.release.version} 已通过校验（${formatBytes(state.verification.archiveSizeBytes)}，SHA-256 ${state.verification.archiveSha256.slice(0, 12)}…）。确认后请完全退出 Obsidian；安装完成会自动重开，设置与旧版备份会保留。`;
    case "confirmed":
      return `已确认 ${state.release.version}。点击安装后，助手只会在 Obsidian 完全退出时替换当前 Vault 的插件，并保留设置和旧版备份。`;
    case "preparing_install":
      return `正在把 ${state.release.version} 写入活动插件目录之外的私有暂存区；当前插件尚未改变。`;
    case "waiting_for_exit":
      return `目标 ${state.targetVersion} 已暂存。请按 Command + Q 完全退出 Obsidian；助手会在退出后安装、校验，并重新打开当前 Vault。`;
    case "loaded_verified":
      return `已从 ${state.previousVersion} 更新到 ${state.currentVersion}；活动 main.js、磁盘 manifest 与本次实际加载版本一致。`;
    case "rolled_back":
      return `目标 ${state.targetVersion} 安装失败，当前仍运行 ${state.currentVersion}；旧版已恢复。诊断码：${state.errorCode}。`;
    case "failed":
      return `${state.message} 活动插件未被修改。`;
  }
}

function formatBytes(value: number): string {
  return value >= 1024 * 1024
    ? `${(value / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.ceil(value / 1024).toString()} KB`;
}

function remoteWorkerDraftError(
  reason: "name_required" | "name_too_long" | "invalid_endpoint"
): string {
  if (reason === "name_required") {
    return "请输入便于识别的设备名称。";
  }
  if (reason === "name_too_long") {
    return "设备名称不能超过 80 个字符。";
  }
  return "请输入有效的私有 HTTPS 地址；地址中不能包含用户名、密码、参数或片段。";
}
