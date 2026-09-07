import { describe, expect, it } from "vitest";

import {
  initialMessageForTask,
  type PendingInitialMessage,
} from "@/lib/assistant/initial-message";

const pending: PendingInitialMessage = { taskId: 7, text: "你好" };

describe("initial message task boundary", () => {
  it("only exposes the message to its owning task", () => {
    expect(initialMessageForTask(pending, 7)).toBe("你好");
    expect(initialMessageForTask(pending, 8)).toBeUndefined();
  });

  it("does not dispatch blank initial input", () => {
    expect(initialMessageForTask({ taskId: 7, text: "  " }, 7)).toBeUndefined();
  });
});
