import { beforeEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ base: "http://127.0.0.1:1", requestJson: vi.fn() }));
vi.mock("@/lib/http/client", () => ({ getApiBaseUrl: () => http.base, requestJson: http.requestJson }));

import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/transport/snapshot-validation";
import { transportState } from "./transport-fixtures";

const approval = (id: string) => ({ id, originSessionId: 2, originTitle: "子 Agent", toolCallId: `call-${id}`, command: "npm i", shell: "sh", cwd: "/w", intent: "装依赖" });

describe("sandbox capability", () => {
  beforeEach(() => {
    vi.resetModules();
    http.requestJson.mockReset();
  });

  it("fetches once per backend and only caches settled results", async () => {
    const { loadSandboxCapability } = await import("@/lib/terminal/sandbox-capability");
    http.requestJson.mockResolvedValueOnce({ supported: false, reason: "timeout", settled: false });
    http.requestJson.mockResolvedValueOnce({ supported: true, reason: "", settled: true });
    expect((await loadSandboxCapability()).settled).toBe(false);
    expect((await loadSandboxCapability()).supported).toBe(true);
    await loadSandboxCapability();
    expect(http.requestJson).toHaveBeenCalledTimes(2);
    // 后端换代（地址变了）重新取：能力是机器级事实，但旧缓存可能来自上一代 Worker。
    http.base = "http://127.0.0.1:2";
    http.requestJson.mockResolvedValueOnce({ supported: true, reason: "", settled: true });
    await loadSandboxCapability();
    expect(http.requestJson).toHaveBeenCalledTimes(3);
  });
});

describe("terminal permission snapshot", () => {
  it("requires the session-level mode and rejects duplicate approval ids", () => {
    expect(parseTransportState(transportState({ terminal_permission_mode: "full_access" })).terminal_permission_mode).toBe("full_access");
    const { terminal_permission_mode: _omitted, ...missing } = transportState();
    expect(() => parseTransportState(missing)).toThrow(TransportSnapshotValidationError);
    expect(() => parseTransportState(transportState({ terminal_approvals: [approval("a"), approval("a")] }))).toThrow(TransportSnapshotValidationError);
    expect(parseTransportState(transportState({ terminal_approvals: [approval("a"), approval("b")] })).terminal_approvals).toHaveLength(2);
  });
});
