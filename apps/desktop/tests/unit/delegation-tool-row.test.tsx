import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DelegationToolRow } from "@/components/assistant-ui/tools/delegation-tool-row";

function renderDelegationRow(runId?: number) {
  const part = {
    toolName: "delegate_task",
    toolCallId: "delegate-1",
    args: {},
    result: undefined,
    artifact: {
      backendStatus: "running",
      presentation: {},
      display_data: {
        kind: "delegation-result",
        title: "审查代码",
        child_task_id: 501,
        child_run_id: 902,
      },
      error: null,
      errorCode: null,
      child_task_id: 501,
      child_run_id: 902,
    },
  } as unknown as ToolCallMessagePartProps;

  return renderToStaticMarkup(<DelegationToolRow {...part} runId={runId} />);
}

describe("DelegationToolRow", () => {
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
