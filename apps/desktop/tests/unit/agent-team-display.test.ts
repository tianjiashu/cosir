import { describe, expect, it } from "vitest";

import {
  readAgentTeamPreviewDisplay,
} from "@/components/conversation/tools/agent-team/agent-team-display";

/** 构造后端预览展示数据（字段名以后端实际下发的契约为准）。 */
function previewDisplayData(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: "agent-team-approval",
    team_run_id: 17,
    team_id: "code-quality",
    name: "代码质量 Team",
    goal: "交付报告",
    node_goals: { develop: { goal: "实现", summary: "实现" } },
    start_node: "develop",
    configuration: { team_id: "code-quality" },
    nodes: [
      {
        node_id: "develop",
        name: "开发",
        agent_id: "general-assistant",
        node_type: "start",
        role: "开发者",
        model_name: "deepseek-chat",
        tools: ["read_file"],
        max_steps: 5,
        statuses: [{ name: "done", color: "#16A34A" }],
      },
    ],
    edges: [],
    ...overrides,
  };
}

describe("agent team 审批卡展示解析", () => {
  it("按后端实际字段名解析预览", () => {
    const preview = readAgentTeamPreviewDisplay(previewDisplayData());

    expect(preview).not.toBeNull();
    expect(preview?.nodes[0]).toMatchObject({
      nodeId: "develop",
      name: "开发",
      role: "开发者",
      maxSteps: 5,
    });
  });

});
