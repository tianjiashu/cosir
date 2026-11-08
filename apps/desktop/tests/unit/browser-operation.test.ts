import { afterEach, describe, expect, it, vi } from "vitest";
import { CdpSession } from "@/electron/main/browser/cdp-session";
import { BrowserOperation } from "@/electron/main/browser/operation";
import { TabEmulation } from "@/electron/main/browser/tab-emulation";
import { BrowserTab } from "@/electron/main/browser/browser-tab";
import type { WebContents } from "electron";
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
vi.mock("electron", () => ({ WebContentsView: class {} }));

function fixture(sendCommand: (method: string, params: Record<string, unknown>) => Promise<unknown>) {
  const contents = { debugger: { sendCommand }, isDestroyed: () => false, isCrashed: () => false } as unknown as WebContents;
  const tab = Object.create(BrowserTab.prototype) as BrowserTab;
  Object.assign(tab, { contents, tabId: "t1", taskId: 1, document: 0, operation: null,
    events: { onChange: vi.fn(), pausedBy: () => null }, noteAgentInput: vi.fn(),
    session: new CdpSession(contents, () => null, () => undefined),
  });
  return tab;
}
const tick = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
afterEach(() => vi.useRealTimers());

describe("BrowserOperation actual command lifetime", () => {
  it("keeps the tab occupied after cancel until the real command resolves, then releases the pressed key once", async () => {
    vi.useFakeTimers();
    let resolve!: () => void;
    const pending = new Promise<void>(r => { resolve = r; });
    const calls: string[] = [];
    const tab = fixture(async (_method, params) => { calls.push(String(params.type)); if (params.type === "keyDown") await pending; return {}; });
    const cancel = new AbortController();
    const operation = new BrowserOperation(tab, cancel.signal);
    tab.operation = operation;
    const typing = operation.inputUnit("Input.dispatchKeyEvent", { type: "keyDown", key: "a" }, { type: "keyUp", key: "a" });
    const rejected = expect(typing).rejects.toThrow("cancel");
    cancel.abort(new Error("cancel")); await rejected;
    const closing = operation.finish();
    await vi.advanceTimersByTimeAsync(1000); await closing;
    expect(tab.operation).toBe(operation); expect(operation.state).toBe("unknown");
    await expect(tab.modify(new AbortController().signal, async () => undefined)).rejects.toMatchObject({ code: "OPERATION_UNKNOWN" });
    operation.resumeCleanup(); operation.resumeCleanup();
    resolve(); await tick();
    expect(calls).toEqual(["keyDown", "keyUp"]);
    expect(tab.operation).toBeNull();
  });

  it("does not release a pressed key into a different document", async () => {
    const calls: string[] = [];
    const tab = fixture(async (_method, params) => { calls.push(String(params.type)); return {}; });
    const operation = new BrowserOperation(tab, new AbortController().signal);
    tab.operation = operation;
    await expect(operation.inputUnit("Input.dispatchKeyEvent", { type: "keyDown" }, { type: "keyUp" }, async () => {
      Object.assign(tab, { document: 1 });
    })).rejects.toMatchObject({ code: "OPERATION_UNKNOWN" });
    await operation.finish();
    expect(calls).toEqual(["keyDown"]); expect(tab.operation).toBe(operation);
    expect(operation.state).toBe("unknown");
  });

  it("records input origin before Electron can synchronously emit its keyboard event", async () => {
    const tab = fixture(async () => { expect(tab.noteAgentInput).toHaveBeenCalledWith("Input.dispatchKeyEvent"); return {}; });
    const operation = new BrowserOperation(tab, new AbortController().signal);
    tab.operation = operation;
    await operation.inputUnit("Input.dispatchKeyEvent", { type: "keyDown" }, { type: "keyUp" });
    await operation.finish(); expect(tab.operation).toBeNull();
  });
  it("resumes a release blocked before dispatch by a dialog exactly once", async () => {
    let paused = false;
    const calls: string[] = [];
    const tab = fixture(async (_method, params) => { calls.push(String(params.type)); return {}; });
    Object.assign(tab, { session: new CdpSession(tab.contents, () => paused ? Object.assign(new Error("dialog open"), { code: "DIALOG_OPEN" }) : null, () => undefined) });
    const operation = new BrowserOperation(tab, new AbortController().signal);
    tab.operation = operation;
    await expect(operation.inputUnit("Input.dispatchMouseEvent", { type: "mousePressed" }, { type: "mouseReleased" }, async () => { paused = true; })).rejects.toMatchObject({ code: "DIALOG_OPEN" });
    await operation.finish();
    expect(operation.state).toBe("unknown"); expect(calls).toEqual(["mousePressed"]);
    // 弹窗答复同时触发设备模拟同步：cleanup 应等它落定，不能丢掉这次恢复事件。
    let resolveSync!: () => void;
    operation.track(new Promise<void>(resolve => { resolveSync = resolve; }));
    paused = false;
    operation.resumeCleanup(); operation.resumeCleanup();
    resolveSync();
    await tick();
    expect(calls).toEqual(["mousePressed", "mouseReleased"]);
    expect(tab.noteAgentInput).toHaveBeenLastCalledWith("Input.dispatchMouseEvent");
    expect(tab.operation).toBeNull();
  });

  it("does not attribute input rejected before dispatch to the agent", async () => {
    const tab = fixture(async () => ({}));
    Object.assign(tab, { session: new CdpSession(tab.contents, () => Object.assign(new Error("dialog open"), { code: "DIALOG_OPEN" }), () => undefined) });
    const operation = new BrowserOperation(tab, new AbortController().signal);
    await expect(operation.inputUnit("Input.dispatchKeyEvent", { type: "keyDown" }, { type: "keyUp" })).rejects.toMatchObject({ code: "DIALOG_OPEN" });
    expect(tab.noteAgentInput).not.toHaveBeenCalled();
  });

  it("serializes emulation through a late raw completion after the CDP wait times out", async () => {
    vi.useFakeTimers();
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    let delay = false;
    const calls: number[] = [];
    const tab = fixture(async (method, params) => {
      if (method === "Emulation.setDeviceMetricsOverride") { calls.push(Number(params.width)); if (delay) { delay = false; await pending; } }
      return {};
    });
    const emulation = new TabEmulation(tab.session.send.bind(tab.session), vi.fn(), vi.fn());
    emulation.show({ x: 0, y: 0, width: 640, height: 400 });
    await emulation.start(new AbortController().signal);
    delay = true;
    const cancel = new AbortController();
    const setting = emulation.set({ viewport: { width: 1280, height: 800, mobile: false } }, cancel.signal);
    await vi.advanceTimersByTimeAsync(10001);
    // 改尺寸不能越过尚未落定的真实命令，即使 CDP 的等待包装已经超时。
    emulation.show({ x: 0, y: 0, width: 320, height: 200 });
    expect(calls).toEqual([640, 1280]);
    release(); await setting; await emulation.settled();
    expect(emulation.current.width).toBe(1280);
    expect(emulation.toInput({ x: 100, y: 20 })).toEqual({ x: 25, y: 5 });
    expect(calls).toEqual([640, 1280, 1280]);
  });

  it("releases each completed input unit even while unrelated observation is pending", async () => {
    const calls: string[] = [];
    const tab = fixture(async (_method, params) => { calls.push(String(params.type)); return {}; });
    const operation = new BrowserOperation(tab, new AbortController().signal);
    tab.operation = operation;
    let settleObservation!: () => void;
    operation.track(new Promise<void>(resolve => { settleObservation = resolve; }));
    for (const key of ["a", "b"]) await operation.inputUnit("Input.dispatchKeyEvent", { type: "keyDown", key }, { type: "keyUp", key });
    expect(calls).toEqual(["keyDown", "keyUp", "keyDown", "keyUp"]);
    settleObservation(); await operation.finish(); expect(tab.operation).toBeNull();
  });

});
