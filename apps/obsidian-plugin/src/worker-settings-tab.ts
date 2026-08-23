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
