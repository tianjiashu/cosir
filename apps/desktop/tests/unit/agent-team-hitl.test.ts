import { describe, expect, it } from "vitest";

import {
  readAgentTeamPreviewDisplay,
  readAgentTeamReviewRequest,
  readAgentTeamRunDisplay,
} from "@/components/assistant-ui/tools/agent-team-display";

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
    parent_task_id: 2,
    parent_run_id: 3,
    workspace_id: 1,
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
    // 待决声明由后端投影在挂起前并进载荷：这里模拟「等待用户作答」的那一份。
    user_input_request: {
      request_id: "17",
      request_kind: "agent_team_review",
      prompt: "确认执行方案",
      decisions: ["approve", "reject"],
      draft_schema: "agent-team-review-v2",
      draft: { goal: "交付报告" },
    },
    ...overrides,
  };
}

describe("agent team human-in-the-loop 展示解析", () => {
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

  it("读取待决请求标识与允许的决定集合", () => {
    const review = readAgentTeamReviewRequest(previewDisplayData());

    expect(review).toEqual({
      requestId: "17",
      decisions: ["approve", "reject"],
      draftSchema: "agent-team-review-v2",
    });
  });

  it("未声明待决或缺少请求标识时不产生可提交的请求", () => {
    expect(readAgentTeamReviewRequest({ kind: "agent-team-approval" })).toBeNull();
    expect(
      readAgentTeamReviewRequest(
        previewDisplayData({ user_input_request: { request_id: "17" } }),
      ),
    ).toBeNull();
  });

  it("区分运行态展示：已批准后不再有可编辑草稿", () => {
    const running = {
      kind: "agent-team-execution",
      team_run_id: 17,
      team_id: "code-quality",
      goal: "交付报告",
    };

    expect(readAgentTeamRunDisplay(running)).toEqual({
      teamRunId: 17,
      teamId: "code-quality",
      goal: "交付报告",
    });
    expect(readAgentTeamPreviewDisplay(running)).toBeNull();
    expect(readAgentTeamRunDisplay(previewDisplayData())).toBeNull();
  });
});
