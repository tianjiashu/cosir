/**
 * 缺陷发现型对抗测试：Run 失败终态的前端渲染通路。
 *
 * 攻击面：
 * 1. `toMessageStatusForRun` 在「status × error × endReason」全组合下的输出；
 * 2. `parseTransportState` 对 Run 级 error 的严格白名单校验（键缺失/多余/空白/非字符串）。
 */
import { describe, expect, it } from "vitest";

import type { TransportRun, TransportState } from "@/lib/assistant/contract";
import { toMessageStatus } from "@/lib/assistant/converter";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";

const assistantMessage = () => ({
  id: "assistant-1",
  role: "assistant" as const,
  parts: [],
});

const run = (overrides: Partial<TransportRun> = {}): TransportRun => ({
  runId: 1,
  status: "failed",
  endReason: null,
  messages: [],
  usage: null,
  error: null,
  ...overrides,
});

describe("toMessageStatusForRun 全组合对抗", () => {
  it("failed + error.message 优先于本地兜底文案", () => {
    const failed = run({
      status: "failed",
      error: { code: "model_insufficient_quota", message: "模型服务配额或余额不足" },
    });
    expect(toMessageStatus(assistantMessage(), failed)).toEqual({
      type: "incomplete",
      reason: "error",
      error: "模型服务配额或余额不足",
    });
  });

  it("failed + error.message 为空字符串时必须回退本地兜底，不得渲染空文案", () => {
    // 潜在缺陷：`error?.message ?? fallback` 对空字符串不生效（?? 只处理 null/undefined），
    // UI 会渲染一个空错误提示。
    const failed = run({ status: "failed", error: { code: "run_failed", message: "" } });
    const status = toMessageStatus(assistantMessage(), failed);
    expect(status).toMatchObject({ type: "incomplete", reason: "error" });
    expect((status as { error?: string }).error).toBeTruthy();
  });

  it("failed + error.message 为纯空白时必须回退本地兜底", () => {
    const failed = run({ status: "failed", error: { code: "run_failed", message: "   " } });
    const status = toMessageStatus(assistantMessage(), failed);
    expect((status as { error?: string }).error).toBeTruthy();
  });

  it("failed + endReason=cancelled 渲染为已取消（即使 error 契约存在）", () => {
    const failed = run({
      status: "failed",
      endReason: "cancelled",
      error: { code: "run_cancelled", message: "已取消本轮对话" },
    });
    expect(toMessageStatus(assistantMessage(), failed)).toEqual({
      type: "incomplete",
      reason: "cancelled",
    });
  });

  it("failed + endReason=client_disconnected 渲染为已取消", () => {
    const failed = run({
      status: "failed",
      endReason: "client_disconnected",
      error: { code: "client_disconnected", message: "连接已断开" },
    });
    expect(toMessageStatus(assistantMessage(), failed)).toEqual({
      type: "incomplete",
      reason: "cancelled",
    });
  });

  it("cancelled 状态即使带 error 契约也必须渲染为已取消，不得显示为错误", () => {
    const cancelled = run({
      status: "cancelled",
      endReason: "user_cancelled",
      error: { code: "model_insufficient_quota", message: "模型服务配额或余额不足" },
    });
    expect(toMessageStatus(assistantMessage(), cancelled)).toEqual({
      type: "incomplete",
      reason: "cancelled",
    });
  });

  it("completed 状态即使携带残留 error 契约也必须渲染为成功", () => {
    // 非终态/成功态不得因残留 error 而把消息渲染成失败。
    const completed = run({
      status: "completed",
      endReason: "stop",
      error: { code: "model_insufficient_quota", message: "残留错误" },
    });
    expect(toMessageStatus(assistantMessage(), completed)).toEqual({
      type: "complete",
      reason: "stop",
    });
  });

  it("running / pending 状态一律渲染为运行中，不受残留 error 影响", () => {
    for (const status of ["running", "pending"]) {
      const active = run({
        status,
        error: { code: "model_insufficient_quota", message: "残留错误" },
      });
      expect(toMessageStatus(assistantMessage(), active)).toEqual({ type: "running" });
    }
  });

  it("interrupted 状态渲染为中断提示，不读取后端 error.message", () => {
    const interrupted = run({
      status: "interrupted",
      error: { code: "run_failed", message: "后端错误" },
    });
    expect(toMessageStatus(assistantMessage(), interrupted)).toEqual({
      type: "incomplete",
      reason: "error",
      error: "上次对话运行已中断，可以继续发送新消息。",
    });
  });
});

describe("parseTransportState Run 级 error 严格校验对抗", () => {
  const baseRun = {
    runId: 1,
    status: "failed",
    endReason: "run_failed",
    usage: null,
    error: null as unknown,
    messages: [],
  };

  const withRun = (error: unknown): TransportState => ({
    runs: [{ ...baseRun, error } as unknown as TransportRun],
    current_run_id: 1,
    approvals: {},
    context_usage_ratio: null,
    context_usage_used: null,
    context_window_total: null,
    error: null,
  });

  it("合法 error 契约通过校验", () => {
    const valid = withRun({ code: "run_failed", message: "通用失败文案" });
    expect(parseTransportState(valid)).toBe(valid);
  });

  it("error 为 undefined（键存在但值缺失）必须被拒绝", () => {
    const invalid = withRun(undefined);
    expect(() => parseTransportState(invalid)).toThrow();
  });

  it("error 缺少 message 键必须被拒绝", () => {
    expect(() => parseTransportState(withRun({ code: "run_failed" }))).toThrow();
  });

  it("error 带多余 retryable 键必须被拒绝", () => {
    expect(() =>
      parseTransportState(withRun({ code: "run_failed", message: "x", retryable: false })),
    ).toThrow();
  });

  it("error.code / error.message 非字符串必须被拒绝", () => {
    expect(() => parseTransportState(withRun({ code: 1, message: "x" }))).toThrow();
    expect(() => parseTransportState(withRun({ code: "x", message: null }))).toThrow();
  });

  it("error 的空白 code / message 必须被拒绝（与后端同口径）", () => {
    // 后端 build_run_error / _validate_error 都要求非空白字符串；前端收紧到同一强度，
    // 否则被绕过的空白文案会在界面上渲染成没有内容的错误气泡。
    expect(() => parseTransportState(withRun({ code: "", message: "" }))).toThrow("runs[0].error");
    expect(() => parseTransportState(withRun({ code: "run_failed", message: "   " }))).toThrow(
      "runs[0].error",
    );
  });

  it("失败态收到空白文案时回退到本地兜底文案", () => {
    expect(toMessageStatus(assistantMessage(), {
      ...run({ status: "failed" }),
      error: { code: "run_failed", message: "   " },
    })).toMatchObject({
      type: "incomplete",
      reason: "error",
      error: "对话运行失败，请检查模型配置或后端状态。",
    });
  });
});
