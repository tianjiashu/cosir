import { describe, expect, it } from "vitest";

import { TASK_RAIL_WIDTH, taskRailPlacement } from "@/lib/task-rail/placement";
import { normalizeTaskRailPreferences, TASK_RAIL_CARD_IDS } from "@/lib/task-rail/store";

describe("会话侧栏偏好", () => {
  it("缺失或非法时用默认值：收起、按登记顺序、全部展开", () => {
    expect(normalizeTaskRailPreferences(undefined)).toEqual({ open: false, order: [...TASK_RAIL_CARD_IDS], collapsed: {} });
    expect(normalizeTaskRailPreferences({ open: "yes", order: "x", collapsed: null })).toEqual({ open: false, order: [...TASK_RAIL_CARD_IDS], collapsed: {} });
  });

  it("丢掉已不存在的卡片与重复项，新卡片补到末尾，折叠只保留合法项", () => {
    expect(normalizeTaskRailPreferences({
      open: true,
      order: ["removed-card", "background-tasks", "background-tasks"],
      collapsed: { "background-tasks": true, "removed-card": true, other: "yes" },
    })).toEqual({ open: true, order: ["background-tasks"], collapsed: { "background-tasks": true } });
    expect(normalizeTaskRailPreferences({ order: [] }).order).toEqual([...TASK_RAIL_CARD_IDS]);
  });
});

describe("会话侧栏摆放", () => {
  it("整行放下侧栏后对话还有 640px 就固定，否则浮起", () => {
    expect(taskRailPlacement(TASK_RAIL_WIDTH + 640)).toBe("docked");
    expect(taskRailPlacement(TASK_RAIL_WIDTH + 639)).toBe("floating");
    expect(taskRailPlacement(0)).toBe("floating");
  });
});
