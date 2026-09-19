import { afterEach, describe, expect, it, vi } from "vitest";

const ui = vi.hoisted(() => ({
  buttons: new Map<string, () => unknown>(),
  labels: [] as string[],
  displays: 0
}));
vi.mock("obsidian", () => {
  const element = () => ({
    empty() { ui.buttons.clear(); ui.labels = []; ui.displays += 1; },
    createEl: () => element(),
    setText() {}
  });
  class Setting {
    setName(value: string) { ui.labels.push(value); return this; }
    setDesc() { return this; }
    addButton(build: (button: unknown) => void) {
      let label = "";
      const button = {
        setButtonText(value: string) { label = value; return button; },
        setCta() { return button; },
        setWarning() { return button; },
        setDisabled() { return button; },
        onClick(callback: () => unknown) { ui.buttons.set(label, callback); return button; }
      };
      build(button);
      return this;
    }
    addText(build: (text: unknown) => void) {
      const text = {
        setPlaceholder() { return text; },
        setValue() { return text; },
        onChange() { return text; }
      };
      build(text);
      return this;
    }
  }
  return {
    App: class {}, Notice: vi.fn(), Setting,
    PluginSettingTab: class { containerEl = element(); }
  };
});
vi.mock("../src/obsidian-worker-transport", () => ({ ObsidianWorkerTransport: class {} }));

import { SpeechCaptureSettingTab } from "../src/worker-settings-tab";

afterEach(() => { vi.useRealTimers(); ui.buttons.clear(); });

function setup() {
  const plugin = {
    settings: { workers: [] },
    preferredWorker: () => ({ id: "synthetic" }),
    credentials: { get: () => "synthetic-token" },
    clientUpdates: {
      state: {
        phase: "awaiting_confirmation",
        release: { version: "0.1.30" },
        verification: { archiveSizeBytes: 1, archiveSha256: "a".repeat(64) }
      } as { phase: string; [key: string]: unknown },
      confirmPreparedUpdate: vi.fn(() => { plugin.clientUpdates.state.phase = "confirmed"; }),
      reset: vi.fn()
    },
    startConfirmedClientUpdate: vi.fn(async () => {
      plugin.clientUpdates.state = { phase: "waiting_for_exit", targetVersion: "0.1.30" };
    }),
    refreshClientUpdateStatus: vi.fn(async () => {})
  };
  const tab = new SpeechCaptureSettingTab({} as never, plugin as never);
  return { plugin, tab };
}

describe("update settings flow", () => {
  it("starts the installer after one explicit confirmation", async () => {
    vi.useFakeTimers();
    const { tab, plugin } = setup();
    tab.display();
    expect(ui.buttons.has("确认此候选包")).toBe(false);
    await ui.buttons.get("确认更新，退出后安装")?.();
    expect(plugin.clientUpdates.confirmPreparedUpdate).toHaveBeenCalledOnce();
    expect(plugin.startConfirmedClientUpdate).toHaveBeenCalledOnce();
    expect(ui.labels).toContain("更新助手正在等待 Obsidian 完全退出");
    tab.hide();
  });

  it("shows a helper failure without requiring an app restart", async () => {
    vi.useFakeTimers();
    const { tab, plugin } = setup();
    plugin.clientUpdates.state = { phase: "waiting_for_exit", targetVersion: "0.1.30" };
    plugin.refreshClientUpdateStatus.mockImplementation(async () => {
      plugin.clientUpdates.state = { phase: "failed", message: "PROCESS_QUERY_FAILED" };
    });
    tab.display();
    await vi.advanceTimersByTimeAsync(1000);
    expect(ui.labels).toContain("更新检查未完成");
    expect(ui.buttons.has("检查更新")).toBe(true);
    const calls = plugin.refreshClientUpdateStatus.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(plugin.refreshClientUpdateStatus).toHaveBeenCalledTimes(calls);
    tab.hide();
  });

  it("does not redraw a hidden page after an in-flight status check", async () => {
    vi.useFakeTimers();
    const { tab, plugin } = setup();
    plugin.clientUpdates.state = { phase: "waiting_for_exit", targetVersion: "0.1.30" };
    let finish!: () => void;
    plugin.refreshClientUpdateStatus.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    tab.display();
    vi.advanceTimersByTime(1000);
    tab.hide();
    const displays = ui.displays;
    plugin.clientUpdates.state = { phase: "failed", message: "failed" };
    finish();
    await vi.advanceTimersByTimeAsync(3000);
    expect(ui.displays).toBe(displays);
    expect(plugin.refreshClientUpdateStatus).toHaveBeenCalledOnce();
  });
});
