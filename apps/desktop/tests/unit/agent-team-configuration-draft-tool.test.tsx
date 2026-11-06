import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentTeamConfigurationTool } from "@/components/assistant-ui/tools/agent-team-configuration-tool";
import { WorkbenchProvider } from "@/lib/workbench/context";

describe("AgentTeamConfigurationTool", () => {
  it.each([true, false])("将未保存的 Team 草稿交给 Workbench 编辑，而不是直接落盘 (interactive=%s)", (interactive) => {
    const markup = renderToStaticMarkup(
      <WorkbenchProvider workspaceId={3}>
        <AgentTeamConfigurationTool
          interactive={interactive}
          artifact={{presentation: "activity",
            backendStatus: "completed",
            awaitingUser: false,
            background: false,
            display_data: {
              kind: "agent-team-configuration-draft",
              status: "draft",
              team_id: "review-team",
              name: "Review Team",
              description: "Review changes",
              scope: "workspace",
              nodes: [{ node_id: "review", name: "Review", agent_id: "reviewer", node_type: "start", statuses: [{ name: "done", color: "#16A34A" }] }],
              edges: [{ from_node_id: "review", status: "done", target_node_id: "END" }],
              configuration: {
                team_id: "review-team",
                name: "Review Team",
                description: "Review changes",
                max_runs: 8,
                start_node_id: "review",
                nodes: [{ node_id: "review", name: "Review", agent_id: "reviewer", statuses: [{ name: "done", color: "#16A34A" }] }],
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

    if (interactive) expect(markup).toContain("编辑配置");
    else expect(markup).not.toContain("编辑配置");
    expect(markup).toContain("未保存草稿");
    expect(markup).not.toContain("保存配置");
  });
});
