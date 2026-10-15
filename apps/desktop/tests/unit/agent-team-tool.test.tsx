import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamTool } from "@/components/assistant-ui/tools/agent-team-tool";

/** 方案预览的完整载荷（字段要求见 readAgentTeamPreviewDisplay 的严格解析）。 */
const PREVIEW: Record<string, unknown> = {
  kind: "agent-team-approval",
  team_run_id: 17,
  team_id: "code-quality",
  name: "交付报告",
  goal: "交付报告",
  start_node: "develop",
  parent_task_id: 3,
  parent_run_id: 2,
  workspace_id: 1,
  configuration: { team_id: "code-quality" },
  node_goals: { develop: { goal: "实现", summary: "实现" } },
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
      statuses: [{ name: "pending", color: "#3B82F6" }],
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
  draft_schema: "agent-team-review-v2",
  draft: { goal: "交付报告" },
};

function renderAgentTeamTool(
  displayData: Record<string, unknown>,
  backendStatus: string = "running",
  runStatus: string = "waiting_for_input",
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

  return renderToStaticMarkup(<AgentTeamTool {...part} runId={2} runStatus={runStatus} taskId={3} />);
}

describe("AgentTeamTool 待确认态", () => {
  it("Run 等待输入且请求有效时显示审批表单，即使 tool part 已完成", () => {
    const html = renderAgentTeamTool({ ...PREVIEW, user_input_request: REVIEW_REQUEST }, "completed");

    expect(html).toContain("等待确认");
    expect(html).not.toContain("执行中");
    // 可编辑表单仍在：等待态提示不能挤掉用户作答的入口。
    expect(html).toContain("确认执行");
    expect(html).toContain("驳回并重新生成");
  });

  it("请求载荷存在但无法识别时显示格式不兼容提示，不回退成执行卡", () => {
    const html = renderAgentTeamTool(
      { ...PREVIEW, user_input_request: { ...REVIEW_REQUEST, draft_schema: "agent-team-review-v0" } },
      "completed",
    );

    expect(html).toContain("审批请求格式不兼容");
    expect(html).not.toContain("Agent Team 执行进度");
    expect(html).not.toContain("确认执行");
  });

  it("Run 未处于等待态时保留审批卡但禁止提交决定", () => {
    // 请求存在决定是否展示审批卡；Run 状态只决定能否提交。
    const html = renderAgentTeamTool(
      { ...PREVIEW, user_input_request: REVIEW_REQUEST },
      "completed",
      "running",
    );

    expect(html).toContain("正在准备确认");
    expect(html).not.toContain("Agent Team 执行进度");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>确认执行<\/button>/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>驳回并重新生成<\/button>/);
  });

  it("执行态由独立执行卡渲染，不再挤进审批卡", () => {
    const html = renderAgentTeamTool({
      ...PREVIEW,
      status: "rejected",
      user_decision: "reject",
    }, "running", "running");

    expect(html).not.toContain("等待确认");
    expect(html).toContain("已驳回");
    expect(html).toContain("主 Agent 正在根据审阅意见重新生成方案");
  });

  it("已同意后从历史消息恢复时显示执行卡，不把旧预览误判为驳回", () => {
    const html = renderAgentTeamTool({ ...PREVIEW, status: "pending" }, "running", "running");

    expect(html).not.toContain("等待确认");
    expect(html).toContain("Agent Team 执行进度");
    expect(html).not.toContain("已驳回");
  });
});
