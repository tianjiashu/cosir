import { describe, expect, it } from "vitest";

import { CONVERSATION_ASIDE_WIDTH, conversationAsidePlacement } from "@/lib/conversation-aside/placement";
import { normalizeConversationAsidePreferences, CONVERSATION_ASIDE_CARD_IDS } from "@/lib/conversation-aside/conversation-aside-store";

describe("会话侧栏偏好", () => {
  it("完整的当前偏好保持用户设置", () => {
    const preferences = { open: true, order: [...CONVERSATION_ASIDE_CARD_IDS], collapsed: { "background-tasks": true } };
    expect(normalizeConversationAsidePreferences(preferences)).toEqual(preferences);
  });
  it("缺失或非法时用默认值：显示、按登记顺序、全部展开", () => {
    expect(normalizeConversationAsidePreferences(undefined)).toEqual({ open: true, order: [...CONVERSATION_ASIDE_CARD_IDS], collapsed: {} });
    expect(normalizeConversationAsidePreferences({ open: "yes", order: "x", collapsed: null })).toEqual({ open: true, order: [...CONVERSATION_ASIDE_CARD_IDS], collapsed: {} });
  });

  it("旧形状整体丢弃，不迁移或保留部分偏好", () => {
    expect(normalizeConversationAsidePreferences({
      open: true,
      order: ["removed-card", "background-tasks", "background-tasks"],
      collapsed: { "background-tasks": true, "removed-card": true, other: "yes" },
    })).toEqual({ open: true, order: [...CONVERSATION_ASIDE_CARD_IDS], collapsed: {} });
    expect(normalizeConversationAsidePreferences({ order: [] }).order).toEqual([...CONVERSATION_ASIDE_CARD_IDS]);
  });
});

describe("会话侧栏摆放", () => {
  it("对话视口放下侧栏后还有 640px 就固定，否则浮起", () => {
    expect(conversationAsidePlacement(CONVERSATION_ASIDE_WIDTH + 640)).toBe("docked");
    expect(conversationAsidePlacement(CONVERSATION_ASIDE_WIDTH + 639)).toBe("floating");
    expect(conversationAsidePlacement(0)).toBe("floating");
  });
});
