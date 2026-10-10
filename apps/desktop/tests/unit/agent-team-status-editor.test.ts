import { describe, expect, it } from "vitest";

import { splitStatusValues } from "@/components/agent-team-status-editor";

describe("AgentTeamStatusEditor", () => {
  it("保留连续、首尾分隔符产生的空状态草稿", () => {
    expect(splitStatusValues(",done, blocked,,")).toEqual(["", "done", " blocked", "", ""]);
  });
});
