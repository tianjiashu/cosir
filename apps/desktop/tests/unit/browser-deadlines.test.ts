import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import deadlines from "@/electron/main/browser/contract/method-deadlines.json";
import type { BrowserHost } from "@/electron/main/browser/browser-host";
import { budgetSignal, deadlineMs, withinDeadline } from "@/electron/main/browser/deadlines";
import { registerBrowserMethods } from "@/electron/main/browser/browser-methods";
import { HostMethodError, HostMethodRegistry } from "@/electron/main/host-link/method-registry";

/** 方法时限：契约文件覆盖每个请求类方法，到点以 `DEADLINE_EXCEEDED` 答复，act 的动作预算提前截止。 */

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("method deadlines", () => {
  it("covers exactly the registered request methods", () => {
    const registered: string[] = [];
    const methods = new HostMethodRegistry();
    vi.spyOn(methods, "register").mockImplementation((method) => void registered.push(method));
    registerBrowserMethods(methods, {} as BrowserHost);
    expect(registered.filter((method) => method !== "browser.dropTasks").sort()).toEqual(Object.keys(deadlines).sort());
  });

  it("answers DEADLINE_EXCEEDED when the work does not finish in time, and stops the work", async () => {
    let seen: AbortSignal | null = null;
    const call = withinDeadline("browser.snapshot", new AbortController().signal, (signal) => {
      seen = signal;
      return new Promise(() => {});
    });
    const outcome = call.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(deadlineMs("browser.snapshot"));
    const error = await outcome;
    expect(error).toBeInstanceOf(HostMethodError);
    expect((error as HostMethodError).code).toBe("DEADLINE_EXCEEDED");
    expect(seen!.aborted).toBe(true);
  });

  it("keeps a cancellation as a cancellation", async () => {
    const controller = new AbortController();
    const reason = new HostMethodError("CANCELLED", "cancelled");
    const call = withinDeadline("browser.snapshot", controller.signal, () => new Promise(() => {}));
    controller.abort(reason);
    await expect(call).rejects.toBe(reason);
  });

  it("ends the act budget early with a DEADLINE_EXCEEDED reason", async () => {
    const signal = budgetSignal("browser.act", 30_000, "out of time");
    await vi.advanceTimersByTimeAsync(deadlineMs("browser.act") - 30_001);
    expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((signal.reason as HostMethodError).code).toBe("DEADLINE_EXCEEDED");
  });
});
