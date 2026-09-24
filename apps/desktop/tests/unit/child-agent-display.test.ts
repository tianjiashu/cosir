import { describe, expect, it } from "vitest";
import {
  readChildAgentResultDisplay,
  readChildAgentWaitDisplay,
} from "@/components/assistant-ui/tools/child-agent-display";

describe("child agent display projections", () => {
  it("reads send/status result payloads without exposing unknown fields", () => {
    expect(readChildAgentResultDisplay({
      kind: "child-agent-result",
      operation: "status",
      child_task_id: 2,
      child_run_id: 3,
      status: "completed",
      agent_id: "reviewer",
      agent_name: "Review",
      final_output: "done",
      end_reason: null,
    })).toMatchObject({ operation: "status", childTaskId: 2, status: "completed", finalOutput: "done" });
    expect(readChildAgentResultDisplay({
      kind: "child-agent-result",
      operation: "status",
      child_task_id: 2,
      child_run_id: 3,
      status: "completed",
      prompt: "must not render",
    })).not.toHaveProperty("prompt");
  });

  it("reads wait terminal end_reason and timeout pending state", () => {
    expect(readChildAgentWaitDisplay({
      kind: "child-agent-wait-result",
      timed_out: false,
      messages: [{ child_task_id: 2, child_run_id: 3, status: "failed", final_output: null, end_reason: "child_failed" }],
      pending: [],
      interrupted_by: null,
    })?.messages[0].endReason).toBe("child_failed");
    expect(readChildAgentWaitDisplay({
      kind: "child-agent-wait-result",
      timed_out: true,
      messages: [],
      pending: [{ child_task_id: 2, status: "running" }],
      interrupted_by: null,
    })?.pending[0].status).toBe("running");
  });
});
