import { describe, expect, it } from "vitest";

import {
  MATCHED_SESSION_LIMIT,
  RECENT_SESSION_LIMIT,
  buildCommandPaletteModel,
  filterCommandPaletteSessions,
} from "@/components/command-palette/command-palette-model";
import { SYSTEM_SETTINGS_TABS } from "@/components/settings/settings-tabs";
import type { WorkspaceTreeSource } from "@/components/session-tree/workspace-tree-model";
import type { WorkspaceSession } from "@/lib/api/workspaces";

function session(id: number, workspaceId: number, title: string, updatedAt: string): WorkspaceSession {
  return {
    session_id: id,
    workspace_id: workspaceId,
    title,
    session_type: "user",

    created_at: updatedAt,
    updated_at: updatedAt,
  };
}

function workspace(id: number, name: string, sessions: WorkspaceSession[]): WorkspaceTreeSource {
  return { workspace_id: id, name, root_path: `/tmp/${name}`, created_at: "", updated_at: "", sessions };
}

const workspaces = [
  workspace(1, "cosir", [
    session(11, 1, "修复登录 Bug", "2026-10-10T08:00:00Z"),
    session(12, 1, "Refactor sidebar", "2026-10-15T08:00:00Z"),
  ]),
  workspace(2, "blog", [
    session(21, 2, "写一篇登录相关文章", "2026-10-12T08:00:00Z"),
  ]),
];

describe("command palette model", () => {
  it("lists sessions across workspaces most recent first, with workspace info", () => {
    const entries = filterCommandPaletteSessions(workspaces, "");
    expect(entries.map((entry) => entry.session.session_id)).toEqual([12, 21, 11]);
    expect(entries[1]).toMatchObject({ workspaceId: 2, workspaceName: "blog" });
  });

  it("matches every term case-insensitively against title and workspace name", () => {
    expect(filterCommandPaletteSessions(workspaces, "登录").map((entry) => entry.session.session_id)).toEqual([21, 11]);
    expect(filterCommandPaletteSessions(workspaces, "COSIR 登录").map((entry) => entry.session.session_id)).toEqual([11]);
    expect(filterCommandPaletteSessions(workspaces, "  sidebar  ").map((entry) => entry.session.session_id)).toEqual([12]);
    expect(filterCommandPaletteSessions(workspaces, "不存在")).toEqual([]);
  });

  it("caps recent sessions without a query and matched sessions with one", () => {
    const many = [workspace(3, "big", Array.from({ length: 50 }, (_, index) =>
      session(100 + index, 3, `会话 ${index}`, new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString()),
    ))];
    const recent = filterCommandPaletteSessions(many, "");
    expect(recent).toHaveLength(RECENT_SESSION_LIMIT);
    expect(recent[0].session.session_id).toBe(149);
    expect(filterCommandPaletteSessions(many, "会话")).toHaveLength(MATCHED_SESSION_LIMIT);
  });

  it("offers the new conversation action and every system settings tab plus context observer", () => {
    const model = buildCommandPaletteModel([], "");
    expect(model.actions).toEqual([expect.objectContaining({ kind: "new-conversation", label: "新会话", shortcut: "newConversation" })]);
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
    expect(buildCommandPaletteModel([], "新建").actions.map((command) => command.label)).toEqual(["新会话"]);
    // 重启后端只在调用方允许时（开发期宿主）列出。
    expect(buildCommandPaletteModel([], "重启").actions).toEqual([]);
    expect(buildCommandPaletteModel([], "重启", { canRestartBackend: true }).actions.map((command) => command.label)).toEqual(["重启后端"]);
    expect(buildCommandPaletteModel(workspaces, "zzz")).toEqual({ sessions: [], actions: [], settings: [] });
  });
});
