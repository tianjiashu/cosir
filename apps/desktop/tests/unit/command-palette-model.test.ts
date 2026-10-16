import { describe, expect, it } from "vitest";

import {
  MATCHED_TASK_LIMIT,
  RECENT_TASK_LIMIT,
  buildCommandPaletteModel,
  filterCommandPaletteTasks,
} from "@/components/command-palette/command-palette-model";
import { SYSTEM_SETTINGS_TABS } from "@/components/settings/settings-tabs";
import type { WorkspaceTreeSource } from "@/components/task-tree/workspace-tree-model";
import type { WorkspaceTask } from "@/lib/api/workspaces";

function task(id: number, workspaceId: number, title: string, updatedAt: string): WorkspaceTask {
  return {
    task_id: id,
    workspace_id: workspaceId,
    title,
    task_type: "user",
    execution_status: null,
    context_window_total: null,
    created_at: updatedAt,
    updated_at: updatedAt,
  };
}

function workspace(id: number, name: string, tasks: WorkspaceTask[]): WorkspaceTreeSource {
  return { workspace_id: id, name, root_path: `/tmp/${name}`, created_at: "", updated_at: "", tasks };
}

const workspaces = [
  workspace(1, "cosir", [
    task(11, 1, "修复登录 Bug", "2026-10-10T08:00:00Z"),
    task(12, 1, "Refactor sidebar", "2026-10-15T08:00:00Z"),
  ]),
  workspace(2, "blog", [
    task(21, 2, "写一篇登录相关文章", "2026-10-12T08:00:00Z"),
  ]),
];

describe("command palette model", () => {
  it("lists tasks across workspaces most recent first, with workspace info", () => {
    const entries = filterCommandPaletteTasks(workspaces, "");
    expect(entries.map((entry) => entry.task.task_id)).toEqual([12, 21, 11]);
    expect(entries[1]).toMatchObject({ workspaceId: 2, workspaceName: "blog" });
  });

  it("matches every term case-insensitively against title and workspace name", () => {
    expect(filterCommandPaletteTasks(workspaces, "登录").map((entry) => entry.task.task_id)).toEqual([21, 11]);
    expect(filterCommandPaletteTasks(workspaces, "COSIR 登录").map((entry) => entry.task.task_id)).toEqual([11]);
    expect(filterCommandPaletteTasks(workspaces, "  sidebar  ").map((entry) => entry.task.task_id)).toEqual([12]);
    expect(filterCommandPaletteTasks(workspaces, "不存在")).toEqual([]);
  });

  it("caps recent tasks without a query and matched tasks with one", () => {
    const many = [workspace(3, "big", Array.from({ length: 50 }, (_, index) =>
      task(100 + index, 3, `任务 ${index}`, new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString()),
    ))];
    const recent = filterCommandPaletteTasks(many, "");
    expect(recent).toHaveLength(RECENT_TASK_LIMIT);
    expect(recent[0].task.task_id).toBe(149);
    expect(filterCommandPaletteTasks(many, "任务")).toHaveLength(MATCHED_TASK_LIMIT);
  });

  it("offers the new conversation action and every system settings tab plus context observer", () => {
    const model = buildCommandPaletteModel([], "");
    expect(model.actions).toEqual([expect.objectContaining({ kind: "new-conversation", label: "新对话", shortcut: "newConversation" })]);
    expect(model.settings.map((command) => command.kind === "navigate" && command.path)).toEqual([
      ...SYSTEM_SETTINGS_TABS.map((tab) => `/settings/${tab.id}`),
      "/context-observer",
    ]);
  });

  it("filters commands by label and keywords", () => {
    expect(buildCommandPaletteModel([], "mcp").settings.map((command) => command.label)).toEqual(["MCP 服务"]);
    expect(buildCommandPaletteModel([], "context").settings.map((command) => command.label)).toEqual(["上下文观测"]);
    const settingsKeyword = buildCommandPaletteModel([], "设置");
    expect(settingsKeyword.settings).toHaveLength(SYSTEM_SETTINGS_TABS.length);
    expect(settingsKeyword.actions).toEqual([]);
    expect(buildCommandPaletteModel([], "新建").actions.map((command) => command.label)).toEqual(["新对话"]);
    expect(buildCommandPaletteModel(workspaces, "zzz")).toEqual({ tasks: [], actions: [], settings: [] });
  });
});
