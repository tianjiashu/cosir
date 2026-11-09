import { describe, expect, it } from "vitest";

import { prepareUserCommand } from "@/lib/assistant/composer/prepare-user-command";
import { createSkillMentionToken, splitSkillMentions } from "@/lib/assistant/composer/skill-mention-token";
import { userMessageSegments } from "@/lib/assistant/turn/user-message-segments";

describe("skill mention token", () => {
  it("is sent to the backend unchanged so it can validate and freeze the skill", () => {
    const text = `按 ${createSkillMentionToken("review-code")} 处理`;
    const command = { type: "add-message", message: { role: "user", parts: [{ type: "text", text }] } };

    expect(prepareUserCommand(command)).toEqual(command);
  });

  it("renders the token from the canonical snapshot text as a chip in place", () => {
    expect(splitSkillMentions("按 [[cosir-skill:review-code]] 处理")).toEqual([
      { type: "text", text: "按 " },
      { type: "token", token: "review-code" },
      { type: "text", text: " 处理" },
    ]);
  });

  it("treats typed bracket text as ordinary text", () => {
    expect(splitSkillMentions("【Skill: review-code】")).toEqual([{ type: "text", text: "【Skill: review-code】" }]);
  });
});

describe("user message segments", () => {
  it("turn skill tokens into skill segments", () => {
    const segments = userMessageSegments([{ type: "text", text: "用 [[cosir-skill:review]]" }]);
    expect(segments).toContainEqual({ type: "skill", name: "review" });
  });
});
