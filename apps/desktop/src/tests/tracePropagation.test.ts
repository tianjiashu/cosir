import { describe, it, expect, afterEach } from "vitest";
import {
  beginClientTrace,
  buildTraceHeaders,
  createClientTrace,
  endClientTrace,
  newTraceId,
  readBackendTraceHeaders,
} from "@/services/tracePropagation";
import { useClientTraceStore } from "@/stores/clientTraceStore";

afterEach(() => {
  useClientTraceStore.getState().reset();
});

describe("tracePropagation", () => {
  it("newTraceId 生成 32 位小写十六进制 trace_id", () => {
    const traceId = newTraceId();

    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
  });

  it("buildTraceHeaders 无显式 trace 时创建并保存当前 trace", () => {
    const result = buildTraceHeaders({ taskId: "task-1" });

    expect(result.headers["x-trace-id"]).toBe(result.trace.traceId);
    expect(result.trace.taskId).toBe("task-1");
    expect(useClientTraceStore.getState().currentTrace?.traceId).toBe(result.trace.traceId);
  });

  it("buildTraceHeaders 复用显式开始的客户端 trace", () => {
    const trace = beginClientTrace({ taskId: "task-1" });
    const result = buildTraceHeaders({ taskId: "task-1" });

    expect(result.trace.traceId).toBe(trace.traceId);
    expect(result.headers["x-trace-id"]).toBe(trace.traceId);

    endClientTrace();
  });

  it("createClientTrace 保留传入的 task/run 关联", () => {
    const trace = createClientTrace({
      taskId: "task-1",
      runId: "run-1",
    });

    expect(trace.traceId).toMatch(/^[0-9a-f]{32}$/);
    expect(trace.taskId).toBe("task-1");
    expect(trace.runId).toBe("run-1");
  });

  it("readBackendTraceHeaders 优先使用后端 x-trace-id，缺失时回退到请求值", () => {
    const response = {
      headers: new Headers({
        "x-trace-id": "1234567890abcdef1234567890abcdef",
      }),
    } as Response;

    const headers = readBackendTraceHeaders(response, "local-trace");

    expect(headers).toEqual({
      traceId: "1234567890abcdef1234567890abcdef",
    });
  });

  it("readBackendTraceHeaders 在响应缺失 x-trace-id 时使用 fallback", () => {
    const response = {
      headers: new Headers(),
    } as Response;

    const headers = readBackendTraceHeaders(response, "local-trace");

    expect(headers).toEqual({ traceId: "local-trace" });
  });
});
