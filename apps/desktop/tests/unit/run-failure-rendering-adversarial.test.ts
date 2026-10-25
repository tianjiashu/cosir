/**
 * 缺陷发现型对抗测试：回复结局的前端渲染通路。
 *
 * 攻击面：
 * 1. `toAssistantMessageStatus` 在「outcome.status × error × endReason」与活动状态全组合下的输出；
 * 2. `parseTransportState` 对 outcome / activity 级 error 的严格白名单校验（键缺失/多余/空白/非字符串）。
 */
import { describe, expect, it } from "vitest";

import type { TransportOutcome } from "@/lib/assistant/contract";
import { toAssistantMessageStatus, type TransportMessageRenderContext } from "@/lib/assistant/converter";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import { activity, assistantMessage, outcome, transportState } from "./transport-fixtures";

const ended = (value: TransportOutcome): TransportMessageRenderContext => ({
  runId: 1, outcome: value, isRunEnd: true, activity: null,
});

describe("toAssistantMessageStatus 全组合对抗", () => {
  it("failed + error.message 优先于本地兜底文案", () => {
    const failed = outcome("failed", { error: { code: "model_insufficient_quota", message: "模型服务配额或余额不足" } });
    expect(toAssistantMessageStatus(ended(failed))).toEqual({ type: "incomplete", reason: "error", error: "模型服务配额或余额不足" });
  });

  it.each(["", "   "])("failed + 空白 error.message（%j）必须回退本地兜底，不得渲染空文案", (message) => {
    const status = toAssistantMessageStatus(ended(outcome("failed", { error: { code: "run_failed", message } })));
    expect(status).toEqual({ type: "incomplete", reason: "error", error: "对话运行失败，请检查模型配置或后端状态。" });
  });

  it.each(["cancelled", "client_disconnected"])("failed + endReason=%s 渲染为已取消（即使 error 契约存在）", (endReason) => {
    const failed = outcome("failed", { endReason, error: { code: "x", message: "已取消本轮对话" } });
    expect(toAssistantMessageStatus(ended(failed))).toEqual({ type: "incomplete", reason: "cancelled" });
  });

  it("cancelled 结局即使带 error 契约也必须渲染为已取消，不得显示为错误", () => {
    const cancelled = outcome("cancelled", { endReason: "user_cancelled", error: { code: "x", message: "残留" } });
    expect(toAssistantMessageStatus(ended(cancelled))).toEqual({ type: "incomplete", reason: "cancelled" });
  });

  it("completed 结局即使携带残留 error 契约也必须渲染为成功", () => {
    const completed = outcome("completed", { error: { code: "x", message: "残留错误" } });
    expect(toAssistantMessageStatus(ended(completed))).toEqual({ type: "complete", reason: "stop" });
  });

  it.each(["running", "suspended"] as const)("没有结局的当前活动回复末尾在 %s 时渲染为运行中", (status) => {
    expect(toAssistantMessageStatus({ runId: 1, outcome: null, isRunEnd: true, activity: activity(1, status) })).toEqual({ type: "running" });
  });

  it("同一 Run 内较早的回复（不是回复末尾）即使活动运行中也已完成", () => {
    expect(toAssistantMessageStatus({ runId: 1, outcome: null, isRunEnd: false, activity: null })).toEqual({ type: "complete", reason: "stop" });
  });

  it("活动已到终态但结局尚未投影到消息时不渲染为运行中", () => {
    expect(toAssistantMessageStatus({ runId: 1, outcome: null, isRunEnd: true, activity: activity(1, "completed") }))
      .toEqual({ type: "complete", reason: "stop" });
  });
});

describe("parseTransportState 结局 error 严格校验对抗", () => {
  const withOutcomeError = (error: unknown) => transportState({
    messages: [assistantMessage("assistant-run-1", 1, [], { ...outcome("failed"), error: error as never })],
  });

  it("合法 error 契约通过校验", () => {
    const valid = withOutcomeError({ code: "run_failed", message: "通用失败文案" });
    expect(parseTransportState(valid)).toBe(valid);
  });

  it("error 为 undefined（键存在但值缺失）必须被拒绝", () => {
    expect(() => parseTransportState(withOutcomeError(undefined))).toThrow();
  });

  it("error 缺少 message 键或带多余 retryable 键必须被拒绝", () => {
    expect(() => parseTransportState(withOutcomeError({ code: "run_failed" }))).toThrow();
    expect(() => parseTransportState(withOutcomeError({ code: "run_failed", message: "x", retryable: false }))).toThrow();
  });

  it("error.code / error.message 非字符串必须被拒绝", () => {
    expect(() => parseTransportState(withOutcomeError({ code: 1, message: "x" }))).toThrow();
    expect(() => parseTransportState(withOutcomeError({ code: "x", message: null }))).toThrow();
  });

  it("error 的空白 code / message 必须被拒绝（与后端同口径）", () => {
    expect(() => parseTransportState(withOutcomeError({ code: "", message: "" }))).toThrow("messages[0].outcome.error");
    expect(() => parseTransportState(withOutcomeError({ code: "run_failed", message: "   " }))).toThrow("messages[0].outcome.error");
  });

  it("活动 error 同样严格校验", () => {
    const invalid = transportState({ activity: activity(1, "failed", { error: { code: "x" } as never }) });
    expect(() => parseTransportState(invalid)).toThrow("activity.error");
  });
});
