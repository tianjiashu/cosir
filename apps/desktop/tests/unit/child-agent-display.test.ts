import { describe, expect, it } from "vitest";
import { readAgentTaskResultDisplay } from "@/components/assistant-ui/tools/child-agent-display";

describe("agent task display projections", () => {
  it("reads send/status payloads without exposing unknown fields", () => {
    expect(readAgentTaskResultDisplay({
      kind: "agent-task-result",
      operation: "status",
      direction: "down",
      task_id: 2,
      agent_name: "Review",
      final_output: "done",
      end_reason: null,
    })).toMatchObject({ operation: "status", direction: "down", taskId: 2, finalOutput: "done" });
    expect(readAgentTaskResultDisplay({
      kind: "agent-task-result",
      operation: "send",
      direction: "up",
      task_id: 2,
      prompt: "must not render",
    })).not.toHaveProperty("prompt");
  });

  it("rejects payloads without a direction or a task", () => {
    expect(readAgentTaskResultDisplay({ kind: "agent-task-result", operation: "send", task_id: 2 })).toBeNull();
    expect(readAgentTaskResultDisplay({ kind: "agent-task-result", operation: "send", direction: "up" })).toBeNull();
  });
});
