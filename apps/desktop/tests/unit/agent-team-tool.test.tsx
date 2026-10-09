import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamTool } from "@/components/assistant-ui/tools/agent-team-tool";

/** 方案预览的完整载荷（字段要求见 readAgentTeamPreviewDisplay 的严格解析）。 */
const PREVIEW: Record<string, unknown> = {
  kind: "agent-team-preview",
  team_run_id: 17,
  team_id: "code-quality",
  name: "交付报告",
  goal: "交付报告",
  start_node: "develop",
  parent_task_id: 3,
  parent_run_id: 2,
  workspace_id: 1,
  configuration: { team_id: "code-quality" },
  node_goals: { develop: "实现" },
  edges: [{ from: "develop", to: "review" }],
  nodes: [
    {
      node_id: "develop",
      name: "实现",
      agent_id: "developer",
      node_type: "start",
      role: "Developer",
      model_name: "deepseek-chat",
      tools: ["read_file"],
      statuses: ["pending"],
      max_steps: 5,
    },
  ],
};

/** 后端在挂起前投影进 tool part 的待决请求载荷。 */
const REVIEW_REQUEST: Record<string, unknown> = {
  request_id: "17",
  request_kind: "agent_team_review",
  prompt: "确认执行方案",
  decisions: ["approve", "reject"],
  draft_schema: "agent-team-review-v1",
  draft: { goal: "交付报告" },
};

function renderAgentTeamTool(
  displayData: Record<string, unknown>,
  backendStatus: string = "running",
): string {
  const part = {
    toolName: "agent_team",
    toolCallId: "call-1",
    args: {},
    result: undefined,
    artifact: {
      backendStatus,
      presentation: {},
      display_data: displayData,
      error: null,
      errorCode: null,
    },
  } as unknown as ToolCallMessagePartProps;

  return renderToStaticMarkup(<AgentTeamTool {...part} runId={2} taskId={3} />);
}

describe("AgentTeamTool 待确认态", () => {
  it("载荷带着待决请求时显示「等待确认」，而不是通用生命周期状态", () => {
    const html = renderAgentTeamTool({ ...PREVIEW, user_input_request: REVIEW_REQUEST });

    expect(html).toContain("等待确认");
    expect(html).not.toContain("执行中");
    // 可编辑表单仍在：等待态提示不能挤掉用户作答的入口。
    expect(html).toContain("确认执行");
    expect(html).toContain("驳回并重新生成");
  });

  it("已提交用户决定后回到生命周期状态展示（同一张卡翻到运行态）", () => {
    const html = renderAgentTeamTool({
      kind: "agent-team-preview",
      status: "running",
      team_run_id: 17,
      team_id: "code-quality",
      goal: "交付报告",
    });

    expect(html).not.toContain("等待确认");
    expect(html).toContain("执行中");
  });

  it("没有待决请求时不显示等待态（避免出现无入口可作答的提示）", () => {
    const html = renderAgentTeamTool({ ...PREVIEW, status: "pending" });

    expect(html).not.toContain("等待确认");
  });
});
