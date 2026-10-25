import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamTool } from "@/components/assistant-ui/tools/agent-team-tool";
import type { AgentTeamRun } from "@/lib/api/agent-teams";
import { createQueryClient } from "@/lib/query/query-client";
import { queryKeys } from "@/lib/query/query-keys";

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

/** 卡片按 TeamRun 状态渲染；静态渲染不发请求，用预置的查询缓存模拟后端返回。 */
function renderAgentTeamTool(teamRun: Partial<AgentTeamRun> | null): string {
  const part = {
    toolName: "agent_team",
    toolCallId: "call-1",
    args: {},
    result: undefined,
    artifact: {
      backendStatus: "completed",
      presentation: {},
      display_data: PREVIEW,
      error: null,
      errorCode: null,
    },
  } as unknown as ToolCallMessagePartProps;
  const client = createQueryClient();
  if (teamRun) {
    client.setQueryData(queryKeys.agentTeamRun(17), {
      id: 17,
      team_id: "code-quality",
      goal: "交付报告",
      status: "pending",
      end_reason: null,
      nodes: [],
      current_node_id: null,
      ...teamRun,
    });
  }
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <AgentTeamTool {...part} taskId={3} />
    </QueryClientProvider>,
  );
}

describe("AgentTeamTool 按 TeamRun 状态渲染", () => {
  it("TeamRun 待确认时显示可提交的审批表单，与工具调用已完成无关", () => {
    const html = renderAgentTeamTool({ status: "pending" });

    expect(html).toContain("等待确认");
    expect(html).not.toContain("Agent Team 执行进度");
    expect(html).toMatch(/<button(?![^>]*disabled="")[^>]*>确认执行<\/button>/);
    expect(html).toMatch(/<button(?![^>]*disabled="")[^>]*>驳回并重新生成<\/button>/);
  });

  it("TeamRun 状态尚未读到时保留审批卡但禁止提交", () => {
    const html = renderAgentTeamTool(null);

    expect(html).toContain("正在同步方案状态");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>确认执行<\/button>/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>驳回并重新生成<\/button>/);
  });

  it("用户驳回后显示驳回结果，不回退成执行卡", () => {
    const html = renderAgentTeamTool({ status: "cancelled", end_reason: "user_rejected" });

    expect(html).toContain("已驳回");
    expect(html).not.toContain("确认执行");
    expect(html).not.toContain("Agent Team 执行进度");
  });

  it("批准后（运行中或已结束）交给执行卡渲染", () => {
    for (const status of ["running", "completed", "cancelled"] as const) {
      const html = renderAgentTeamTool({ status, end_reason: status === "cancelled" ? "cancelled" : null });

      expect(html).not.toContain("等待确认");
      expect(html).not.toContain("已驳回");
      expect(html).toContain("Agent Team 执行进度");
    }
  });
});
