import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DelegationToolRow } from "@/components/assistant-ui/tools/delegation-tool-row";

function renderDelegationRow(runId?: number, displayData: Record<string, unknown> = {
  kind: "delegation-result",
  title: "审查代码",
  child_task_id: 501,
  child_run_id: 902,
  status: "running",
  role: "Reviewer",
}) {
  const part = {
    toolName: "delegate_task",
    toolCallId: "delegate-1",
    args: {},
    result: undefined,
    artifact: {
      backendStatus: "running",
      presentation: {},
      display_data: displayData,
      error: null,
      errorCode: null,
      child_task_id: 501,
      child_run_id: 902,
    },
  } as unknown as ToolCallMessagePartProps;

  return renderToStaticMarkup(<DelegationToolRow {...part} runId={runId} />);
}

describe("DelegationToolRow", () => {
  it("does not duplicate child locators or lifecycle text in the compact row", () => {
    const html = renderDelegationRow(undefined, {
      kind: "delegation-result",
      title: "审查代码",
      child_agent_id: "delegate_reviewer",
      child_task_id: 501,
      child_run_id: 902,
      role: "Reviewer",
      status: "running",
      prompt: "不应显示的原始 prompt",
      error: "Traceback: secret=do-not-render",
    });

    expect(html).toContain("Reviewer");
    expect(html).toContain("执行中");
    expect(html).not.toContain("task 501");
    expect(html).not.toContain("run 902");
    expect(html).not.toContain("运行中");
    expect(html).not.toContain("不应显示的原始 prompt");
    expect(html).not.toContain("Traceback: secret=do-not-render");
  });

  it("keeps child output out of the parent delegation row", () => {
    const html = renderDelegationRow(undefined, {
      kind: "delegation-result",
      title: "审查代码",
      child_task_id: 501,
      child_run_id: 903,
      role: "Reviewer",
      status: "completed",
      prompt: "隐藏 prompt",
      error: "raw provider exception",
    });

    expect(html).toContain("已完成");
    expect(html).not.toContain("隐藏 prompt");
    expect(html).not.toContain("raw provider exception");
  });

  it.each([
    ["failed", "失败"],
    ["cancelled", "已取消"],
  ] as const)("projects %s child state with a stable label", (status, label) => {
    const html = renderDelegationRow(undefined, {
      kind: "delegation-result",
      title: "审查代码",
      child_task_id: 501,
      child_run_id: 904,
      role: "Reviewer",
      status,
      error: "raw exception must stay hidden",
    });

    expect(html).toContain(label);
    expect(html).not.toContain("raw exception must stay hidden");
  });

  it("offers child cancellation in the main Thread when both locators are available", () => {
    const html = renderDelegationRow(41);

    expect(html).toContain('aria-label="停止子 Agent：审查代码"');
    expect(html).toContain(">停止</button>");
  });

  it("keeps child cancellation unavailable on read-only surfaces", () => {
    const html = renderDelegationRow();

    expect(html).not.toContain("停止子 Agent");
    expect(html).toContain("审查代码");
  });
});
