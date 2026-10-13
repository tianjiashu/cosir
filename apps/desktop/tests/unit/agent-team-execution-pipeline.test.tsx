import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamExecutionPipeline } from "@/components/assistant-ui/tools/agent-team-execution-pipeline";
import { AgentTeamExecutionContent } from "@/components/assistant-ui/tools/agent-team-execution-card";
import type { AgentTeamRun } from "@/lib/api/agent-teams";

describe("Agent Team 失败流水线", () => {
  it("显示失败节点、原因及未到达的终点", () => {
    const run: AgentTeamRun = {
      id: 17, team_id: "quality", status: "failed", goal: "完成审查", current_node_id: null,
      nodes: [
        {
          node_id: "implement", name: "实现", agent_id: "developer", role: "开发",
          goal_summary: "实现", status: "completed",
          visits: [{
            visit_number: 1, conversation_run_id: 11, status: "completed", agents: [],
          }],
        },
        {
          node_id: "review", name: "审查", agent_id: "reviewer", role: "审查",
          goal_summary: "审查", status: "failed",
          visits: [{
            visit_number: 1, conversation_run_id: 12, status: "failed", agents: [],
          }],
        },
      ],
    };

    const html = renderToStaticMarkup(
      <AgentTeamExecutionPipeline run={run} onOpenAgent={() => undefined} />,
    );

    expect(html).toContain("失败</span>");
    expect(html).toContain("未到达");
    expect(html).toContain("overflow-x-auto");

    const card = renderToStaticMarkup(
      <AgentTeamExecutionContent
        display={{ teamRunId: 17, teamId: "quality", goal: "" }}
        run={run}
        backendStatus="failed"
        onOpenAgent={() => undefined}
      />,
    );
    expect(card).toContain("审查");
    expect(card).toContain("执行失败");
    expect(card).toContain("quality");
  });
});
