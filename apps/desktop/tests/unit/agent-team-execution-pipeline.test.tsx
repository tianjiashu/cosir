import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamExecutionPipeline } from "@/components/assistant-ui/tools/agent-team-execution-pipeline";
import { AgentTeamExecutionContent } from "@/components/assistant-ui/tools/agent-team-execution-card";
import type { AgentTeamRun } from "@/lib/api/agent-teams";

describe("Agent Team 失败流水线", () => {
  it("显示失败节点、原因及未到达的终点", () => {
    const run: AgentTeamRun = {
      id: 17, team_id: "quality", workspace_id: 1, parent_task_id: 2,
      parent_run_id: 3, status: "failed", goal: "完成审查", start_node_id: "implement",
      current_node_id: null, failure_node_id: "review", end_reason: "node_run_failed",
      started_at: null, ended_at: null,
      nodes: [
        {
          node_id: "implement", name: "实现", agent_id: "developer", role: "开发", statuses: [],
          goal: "实现", goal_summary: "实现", task_id: 8, status: "completed",
          visits: [{
            visit_number: 1, conversation_run_id: 11, status: "completed", business_status: "done",
            business_status_color: "#16A34A", failure_code: null, failure_message: null, agents: [],
          }],
        },
        {
          node_id: "review", name: "审查", agent_id: "reviewer", role: "审查", statuses: [],
          goal: "审查", goal_summary: "审查", task_id: 9, status: "failed",
          visits: [{
            visit_number: 1, conversation_run_id: 12, status: "failed", business_status: null,
            business_status_color: null, failure_code: "model_resolve_failed",
            failure_message: "模型配置不可用，请检查模型设置", agents: [],
          }],
        },
      ],
    };

    const html = renderToStaticMarkup(
      <AgentTeamExecutionPipeline run={run} onOpenAgent={() => undefined} />,
    );

    expect(html).toContain("模型配置不可用，请检查模型设置");
    expect(html).toContain("失败</span>");
    expect(html).toContain("未到达");
    expect(html).toContain("overflow-x-auto");

    const card = renderToStaticMarkup(
      <AgentTeamExecutionContent
        display={{ teamRunId: 17, teamId: "quality", goal: "" }}
        run={run}
        error={null}
        backendStatus="failed"
        onOpenAgent={() => undefined}
      />,
    );
    expect(card).toContain("节点「审查」失败");
    expect(card).toContain("模型配置不可用，请检查模型设置");
    expect(card).toContain("TeamRun #17");
    expect(card).toContain("节点 Run #12");
  });
});
