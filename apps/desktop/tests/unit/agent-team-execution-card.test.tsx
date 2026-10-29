import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  AgentTeamExecutionContent,
  canStopTeamRun,
} from "@/components/assistant-ui/tools/agent-team-execution-card";
import type { AgentTeamRun } from "@/lib/api/agent-teams";

function teamRun(status: AgentTeamRun["status"], endReason: string | null = null): AgentTeamRun {
  return {
    id: 17, team_id: "quality", status, goal: "完成审查", current_node_id: null,
    end_reason: endReason, nodes: [],
  };
}

function renderCard(run: AgentTeamRun, props: Partial<Parameters<typeof AgentTeamExecutionContent>[0]> = {}) {
  return renderToStaticMarkup(
    <AgentTeamExecutionContent
      display={{ teamRunId: 17, teamId: "quality", goal: "" }}
      run={run}
      backendStatus="running"
      pollError={null}
      onRetryPoll={() => undefined}
      onOpenAgent={() => undefined}
      {...props}
    />,
  );
}

describe("单独停止 Agent Team", () => {
  it("只在运行中、可写且未在停止整个 Task 时提供停止入口", () => {
    expect(canStopTeamRun(teamRun("running"), true, false)).toBe(true);
    expect(canStopTeamRun(teamRun("running"), false, false)).toBe(false);
    expect(canStopTeamRun(teamRun("running"), true, true)).toBe(false);
    // 待确认由审批卡的「驳回」处理，终态没有可停的对象。
    expect(canStopTeamRun(teamRun("pending"), true, false)).toBe(false);
    expect(canStopTeamRun(teamRun("completed"), true, false)).toBe(false);
    expect(canStopTeamRun(null, true, false)).toBe(false);
  });

  it("可停止时在表头渲染停止按钮，停止中显示进行态", () => {
    const idle = renderCard(teamRun("running"), { canStop: true, onStop: () => undefined });
    expect(idle).toContain('aria-label="停止Agent Team"');

    const stopping = renderCard(teamRun("running"), { canStop: true, stopping: true, onStop: () => undefined });
    expect(stopping).toContain('aria-label="正在停止Agent Team"');
    expect(stopping).toContain("disabled");
  });

  it("不可停止时不渲染按钮", () => {
    expect(renderCard(teamRun("running"))).not.toContain("停止Agent Team");
  });

  it("停止请求失败时在卡片内提示原因", () => {
    const html = renderCard(teamRun("running"), {
      canStop: true, stopError: "网络异常", onStop: () => undefined,
    });
    expect(html).toContain("停止 Agent Team 失败：网络异常");
  });

  it("取消按原因区分「已停止」与「随对话停止」", () => {
    expect(renderCard(teamRun("cancelled", "cancelled"))).toContain("已停止");
    expect(renderCard(teamRun("cancelled", "parent_cancelled"))).toContain("随对话停止");
    expect(renderCard(teamRun("cancelled", "runtime_restarted"))).toContain("已取消");
  });
});
