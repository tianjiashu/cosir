/**
 * 回归测试：run_failed 差异化文案解析（resolveRunFailedText）。
 *
 * 背景：Agent 达到最大步骤数但未产出最终回答时，model 节点统一收口，在 run_failed 的
 * payload 中下发 end_reason="max_steps_reached"（对齐 client_disconnected 的差异化文案
 * 模式）。StatusBadge 应据此渲染「已达到最大步骤数，未产出最终回答」的可读说明，而非
 * 只展示 max_steps_reached 枚举码。判定逻辑收敛为纯函数 resolveRunFailedText，可直接单测
 * （避免依赖受 React 构建模式影响的组件 render）。
 *
 * @module tests/statusBadge.maxStepsReached
 */

import { describe, expect, it } from "vitest";

import { resolveRunFailedText } from "@/components/chat/StatusBadge";

describe("resolveRunFailedText：run_failed 差异化失败文案解析", () => {
  it("max_steps_reached 时返回可读失败说明，而非枚举码", () => {
    const text = resolveRunFailedText({
      error: "max_steps_reached",
      end_reason: "max_steps_reached",
      step_id: "step-1",
    });
    expect(text).toContain("已达到最大步骤数");
    expect(text).not.toContain("max_steps_reached");
  });

  it("client_disconnected 时返回连接中断说明（既有行为保持）", () => {
    const text = resolveRunFailedText({
      error: "x",
      end_reason: "client_disconnected",
      step_id: "step-1",
    });
    expect(text).toContain("连接已中断");
  });

  it("普通 run_failed（无 end_reason）返回 error 原文", () => {
    const text = resolveRunFailedText({ error: "boom", step_id: "step-1" });
    expect(text).toBe("boom");
  });

  it("error 缺失时返回 undefined（调用方决定是否渲染）", () => {
    const text = resolveRunFailedText({ end_reason: "some_other", step_id: "step-1" });
    expect(text).toBeUndefined();
  });

  it("其它语义化 end_reason（非 client_disconnected / max_steps_reached）时返回 error 原文", () => {
    const text = resolveRunFailedText({
      error: "invalid_tool_call",
      end_reason: "invalid_tool",
      step_id: "step-1",
    });
    expect(text).toBe("invalid_tool_call");
  });

  it("client_disconnected 且 error 缺失时仍返回连接中断说明（枚举优先于 error 缺失）", () => {
    const text = resolveRunFailedText({ end_reason: "client_disconnected", step_id: "step-1" });
    expect(text).toContain("连接已中断");
  });

  it("空 payload（无 error 也无 end_reason）时返回 undefined", () => {
    const text = resolveRunFailedText({});
    expect(text).toBeUndefined();
  });

  it("max_steps_reached 且 error 缺失时仍返回步数说明（枚举优先于 error 缺失）", () => {
    const text = resolveRunFailedText({ end_reason: "max_steps_reached" });
    expect(text).toContain("已达到最大步骤数");
  });
});
