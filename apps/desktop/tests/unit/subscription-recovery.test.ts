import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SubscriptionRecovery } from "@/components/assistant/runtime/subscription-recovery";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn(async () => undefined) }));

function setup() {
  const attach = vi.fn(async () => undefined);
  const backend = { available: true, generation: 1 };
  const setIssue = vi.fn();
  const recovery = new SubscriptionRecovery({ taskId: 7, traceId: "t", attach: () => attach, backend: () => backend, setIssue });
  recovery.setActive(true);
  return { attach, backend, setIssue, recovery };
}

describe("SubscriptionRecovery 有限重连（D4）", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("意外断开按延迟重开订阅，连续失败耗尽后提示用户重新同步", () => {
    const { attach, setIssue, recovery } = setup();
    for (let attempt = 0; attempt < 5; attempt += 1) {
      recovery.reconnect("disconnected");
      vi.advanceTimersByTime(2_000);
    }
    expect(attach).toHaveBeenCalledTimes(5);
    recovery.reconnect("disconnected");
    vi.advanceTimersByTime(2_000);
    expect(attach).toHaveBeenCalledTimes(5);
    expect(setIssue).toHaveBeenLastCalledWith(expect.objectContaining({ message: "本机后端连接多次中断，请重新同步恢复对话。" }));
  });

  it("订阅建立后撤下待执行的重连与延迟提示", () => {
    const { attach, setIssue, recovery } = setup();
    recovery.reconnect("disconnected");
    recovery.markConnected();
    vi.advanceTimersByTime(2_000);
    expect(attach).not.toHaveBeenCalled();
    expect(setIssue).not.toHaveBeenCalled();
  });

  it("attach 成功后立刻被关闭仍计为连续失败，不退化成无限重连", () => {
    const { attach, recovery } = setup();
    for (let attempt = 0; attempt < 8; attempt += 1) {
      recovery.reconnect("disconnected");
      vi.advanceTimersByTime(2_000);
      recovery.markConnected();
    }
    expect(attach).toHaveBeenCalledTimes(5);
  });

  it("连接稳定保持一段时间后再断开才清零预算", () => {
    const { attach, recovery } = setup();
    for (let attempt = 0; attempt < 5; attempt += 1) {
      recovery.reconnect("disconnected");
      vi.advanceTimersByTime(2_000);
    }
    recovery.markConnected();
    vi.advanceTimersByTime(3_000);
    recovery.reconnect("disconnected");
    vi.advanceTimersByTime(2_000);
    expect(attach).toHaveBeenCalledTimes(6);
  });

  it("背压重同步立即重开、不消耗预算", () => {
    const { attach, recovery } = setup();
    for (let index = 0; index < 10; index += 1) recovery.reconnect("resync_required");
    expect(attach).toHaveBeenCalledTimes(10);
  });

  it("后端不可用或计时期间换了实例时不重连", () => {
    const { attach, backend, recovery } = setup();
    recovery.reconnect("disconnected");
    backend.generation = 2;
    vi.advanceTimersByTime(2_000);
    expect(attach).not.toHaveBeenCalled();

    backend.available = false;
    recovery.reconnect("disconnected");
    vi.advanceTimersByTime(2_000);
    expect(attach).not.toHaveBeenCalled();
  });

  it("显式重新同步清零预算后立即重开订阅；卸载后不再调度", () => {
    const { attach, recovery } = setup();
    recovery.resync(null);
    expect(attach).toHaveBeenCalledTimes(1);
    recovery.setActive(false);
    recovery.reconnect("disconnected");
    vi.advanceTimersByTime(2_000);
    expect(attach).toHaveBeenCalledTimes(1);
  });

  it("后端恢复只清零预算并提示，不重开订阅（runtime 按新实例自行打开）", () => {
    const { attach, setIssue, recovery } = setup();
    for (let attempt = 0; attempt < 5; attempt += 1) {
      recovery.reconnect("disconnected");
      vi.advanceTimersByTime(2_000);
    }
    attach.mockClear();
    const issue = { message: "本机后端已恢复，正在同步当前对话…", canResync: true };
    recovery.restored(issue);
    expect(attach).not.toHaveBeenCalled();
    expect(setIssue).toHaveBeenLastCalledWith(issue);
    // 预算已清零：之后的断开重新获得完整重连次数。
    recovery.reconnect("disconnected");
    vi.advanceTimersByTime(2_000);
    expect(attach).toHaveBeenCalledTimes(1);
  });
});
