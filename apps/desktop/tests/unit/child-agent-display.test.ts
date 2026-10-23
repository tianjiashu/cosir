import { describe, expect, it } from "vitest";
import { readChildAgentResultDisplay } from "@/components/assistant-ui/tools/child-agent-display";

describe("child agent display projections", () => {
  it("reads send/status result payloads without exposing unknown fields", () => {
    expect(readChildAgentResultDisplay({
      kind: "child-agent-result",
      operation: "status",
      child_task_id: 2,
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
      status: "completed",
      prompt: "must not render",
    })).not.toHaveProperty("prompt");
  });

});
