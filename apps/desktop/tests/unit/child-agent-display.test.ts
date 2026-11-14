import { describe, expect, it } from "vitest";
import { readAgentSessionResultDisplay } from "@/components/conversation/tools/child-agent-display";

describe("agent session display projections", () => {
  it("reads send/status payloads without exposing unknown fields", () => {
    expect(readAgentSessionResultDisplay({
      kind: "agent-session-result",
      operation: "status",
      session_id: 2,
      agent_name: "Review",
      final_output: "done",
      end_reason: null,
    })).toMatchObject({ operation: "status", sessionId: 2, finalOutput: "done" });
    expect(readAgentSessionResultDisplay({
      kind: "agent-session-result",
      operation: "send",
      session_id: 2,
      prompt: "must not render",
    })).not.toHaveProperty("prompt");
  });

  it("requires a session id for sending", () => {
    expect(readAgentSessionResultDisplay({ kind: "agent-session-result", operation: "send", session_id: 2 })).toMatchObject({ sessionId: 2 });
    expect(readAgentSessionResultDisplay({ kind: "agent-session-result", operation: "send" })).toBeNull();
  });
});
