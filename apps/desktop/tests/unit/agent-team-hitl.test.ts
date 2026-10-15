import { describe, expect, it, vi } from "vitest";

import {
  readAgentTeamPreviewDisplay,
  readAgentTeamReviewRequest,
  readAgentTeamRunDisplay,
} from "@/components/assistant-ui/tools/agent-team-display";
import { submitUserInputDecision } from "@/lib/assistant/submit-user-input-decision";
import { HttpError } from "@/lib/http/errors";

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

describe("submitUserInputDecision", () => {
  it("把决定作为续跑命令提交，并携带 runId 定位等待中的 Run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 200 })),
    );

    await expect(
      submitUserInputDecision({
        taskId: 7,
        runId: 31,
        decision: {
          request_id: "17",
          decision: "approve",
          data: { goal: "交付报告", node_goals: { develop: "实现" } },
        },
      }),
    ).resolves.toBeUndefined();

    expect(fetch).toHaveBeenCalledOnce();
    const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as {
      taskId: number;
      runId: number;
      threadId: string;
      commands: Array<{ name: string; payload: { decision: unknown }; commandId: string }>;
    };
    expect(body.taskId).toBe(7);
    expect(body.runId).toBe(31);
    expect(body.threadId).toBe("task-7");
    expect(body.commands).toHaveLength(1);
    expect(body.commands[0].name).toBe("user-input-decision");
    expect(body.commands[0].payload.decision).toEqual({
      request_id: "17",
      decision: "approve",
      data: { goal: "交付报告", node_goals: { develop: "实现" } },
    });
    vi.unstubAllGlobals();
  });

  it("后端拒绝时抛出 HttpError，不把失败当成功，也不透出响应体原文", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("run is not resumable", { status: 409 })),
    );

    const error = await submitUserInputDecision({
      taskId: 7,
      runId: 31,
      decision: { request_id: "17", decision: "approve" },
    }).catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).status).toBe(409);
    expect((error as HttpError).message).not.toContain("run is not resumable");
    vi.unstubAllGlobals();
  });
});
