import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamConfigurationTool } from "@/components/assistant-ui/tools/agent-team-configuration-tool";
import { WorkbenchProvider } from "@/lib/workbench/context";

describe("AgentTeamConfigurationTool", () => {
  it("将未保存的 Team 草稿交给 Workbench 编辑，而不是直接落盘", () => {
    const markup = renderToStaticMarkup(
      <WorkbenchProvider workspaceId={3}>
        <AgentTeamConfigurationTool
          type="tool-call"
          toolName="propose_agent_team_configuration"
          args={{}}
          argsText="{}"
          status={{ type: "complete", reason: "stop" } as never}
          addResult={() => undefined}
          resume={() => undefined}
          respondToApproval={() => undefined}
          artifact={{
            backendStatus: "completed",
            presentation: {},
            display_data: {
              kind: "agent-team-configuration-draft",
              status: "draft",
              team_id: "review-team",
              name: "Review Team",
              description: "Review changes",
              scope: "workspace",
              nodes: [{ node_id: "review", name: "Review", agent_id: "reviewer", node_type: "start", statuses: ["done"] }],
              edges: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
              configuration: {
                team_id: "review-team",
                name: "Review Team",
                description: "Review changes",
                max_runs: 8,
                start_node_id: "review",
                nodes: [{ node_id: "review", name: "Review", agent_id: "reviewer", statuses: ["done"] }],
                transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
                scope: "workspace",
              },
            },
            error: null,
            errorCode: null,
          }}
          taskId={4}
          toolCallId="team-draft-1"
        />
      </WorkbenchProvider>,
    );

    expect(markup).toContain("编辑配置");
    expect(markup).toContain("未保存草稿");
    expect(markup).not.toContain("保存配置");
  });
});
