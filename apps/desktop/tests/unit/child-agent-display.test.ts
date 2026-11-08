import { describe, expect, it } from "vitest";
import { readAgentTaskResultDisplay } from "@/components/conversation/tools/child-agent-display";

describe("agent task display projections", () => {
  it("reads send/status payloads without exposing unknown fields", () => {
    expect(readAgentTaskResultDisplay({
      kind: "agent-task-result",
      operation: "status",
      task_id: 2,
      agent_name: "Review",
      final_output: "done",
      end_reason: null,
    })).toMatchObject({ operation: "status", taskId: 2, finalOutput: "done" });
    expect(readAgentTaskResultDisplay({
      kind: "agent-task-result",
      operation: "send",
      task_id: 2,
      prompt: "must not render",
    })).not.toHaveProperty("prompt");
  });

  it("requires a task id for sending", () => {
    expect(readAgentTaskResultDisplay({ kind: "agent-task-result", operation: "send", task_id: 2 })).toMatchObject({ taskId: 2 });
    expect(readAgentTaskResultDisplay({ kind: "agent-task-result", operation: "send" })).toBeNull();
  });
});
