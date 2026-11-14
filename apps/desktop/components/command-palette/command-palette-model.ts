import type { LucideIcon } from "lucide-react";
import { ActivityIcon, RotateCwIcon, SquarePenIcon } from "lucide-react";

import { SYSTEM_SETTINGS_TABS } from "@/components/settings/settings-tabs";
import type { WorkspaceTreeSource } from "@/components/session-tree/workspace-tree-model";
import type { WorkspaceSession } from "@/lib/api/workspaces";
import type { AppShortcutId } from "@/lib/keyboard/shortcuts";

/**
 * 命令面板的纯数据模型：把工作区会话、固定操作与配置入口按查询词过滤、排序并截断。
 *
 * 过滤在这里而不是交给 cmdk（面板以 `shouldFilter={false}` 使用 cmdk）：cmdk 的模糊打分会
 * 打乱「最近更新优先」的会话顺序，也无法在过滤后再截断条数；会话数可达数百，全部交给 cmdk
 * 渲染再过滤会让每次按键都重排整棵列表。
 *
 * 匹配规则：查询按空白拆词，每个词（忽略大小写）都须出现在条目的可搜索文本中（会话是标题 +
 * 工作区名，其余是名称 + 关键词），因此「cosir 修复」能同时按工作区与标题收窄。
 */

export type CommandPaletteSessionEntry = {
  session: WorkspaceSession;
  workspaceId: number;
  workspaceName: string;
};

/** 面板内的固定命令。`kind` 决定选中后回调哪一个 prop；`shortcut` 指向 lib/keyboard/shortcuts 中的登记项，用于展示组合键。 */
export type CommandPaletteCommand = { id: string; label: string; icon: LucideIcon; shortcut?: AppShortcutId } & (
  | { kind: "new-conversation" }
  | { kind: "restart-backend" }
  | { kind: "navigate"; path: string }
);

/** keywords 只参与匹配，不随命令交给渲染层。 */
type SearchableCommand = { command: CommandPaletteCommand; keywords: readonly string[] };

export type CommandPaletteModel = {
  sessions: CommandPaletteSessionEntry[];
  actions: CommandPaletteCommand[];
  settings: CommandPaletteCommand[];
};

/** 无查询词时只列最近更新的几条，作为「快速回到最近会话」；有查询词时放宽但仍截断，避免长列表。 */
export const RECENT_SESSION_LIMIT = 8;
export const MATCHED_SESSION_LIMIT = 30;

const ACTION_COMMANDS: readonly SearchableCommand[] = [
  {
    command: { kind: "new-conversation", id: "action:new-conversation", label: "新会话", icon: SquarePenIcon, shortcut: "newConversation" },
    keywords: ["新建", "会话", "new", "conversation", "chat"],
  },
];

/** 少用且会中断运行中会话的操作，只在调用方允许时（开发期宿主）列出，不常驻页头。 */
const RESTART_BACKEND_COMMAND: SearchableCommand = {
  command: { kind: "restart-backend", id: "action:restart-backend", label: "重启后端", icon: RotateCwIcon },
  keywords: ["重启", "后端", "restart", "backend"],
};

const SETTINGS_COMMANDS: readonly SearchableCommand[] = [
  ...SYSTEM_SETTINGS_TABS.map((tab): SearchableCommand => ({
    command: { kind: "navigate", id: `settings:${tab.id}`, label: tab.label, path: `/settings/${tab.id}`, icon: tab.icon },
    keywords: ["配置", "设置", "系统配置", "settings", tab.id],
  })),
  {
    command: { kind: "navigate", id: "page:session-observer", label: "会话观测", path: "/session-observer", icon: ActivityIcon },
    keywords: ["上下文", "观测", "context", "observer"],
  },
];

function queryTerms(query: string): string[] {
  return query.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

function matchesAll(terms: string[], haystack: string): boolean {
  const text = haystack.toLowerCase();
  return terms.every((term) => text.includes(term));
}

function filterCommands(commands: readonly SearchableCommand[], terms: string[]): CommandPaletteCommand[] {
  return commands
    .filter(({ command, keywords }) => matchesAll(terms, [command.label, ...keywords].join(" ")))
    .map(({ command }) => command);
}

function updatedAtMs(session: WorkspaceSession): number {
  const value = Date.parse(session.updated_at);
  return Number.isNaN(value) ? 0 : value;
}

export function filterCommandPaletteSessions(workspaces: WorkspaceTreeSource[], query: string): CommandPaletteSessionEntry[] {
  const terms = queryTerms(query);
  const matched = workspaces.flatMap((workspace) =>
    workspace.sessions
      .filter((session) => matchesAll(terms, `${session.title} ${workspace.name}`))
      .map((session) => ({ session, workspaceId: workspace.workspace_id, workspaceName: workspace.name })),
  );
  matched.sort((a, b) => updatedAtMs(b.session) - updatedAtMs(a.session));
  return matched.slice(0, terms.length > 0 ? MATCHED_SESSION_LIMIT : RECENT_SESSION_LIMIT);
}

export function buildCommandPaletteModel(
  workspaces: WorkspaceTreeSource[],
  query: string,
  { canRestartBackend = false }: { canRestartBackend?: boolean } = {},
): CommandPaletteModel {
  const terms = queryTerms(query);
  return {
    sessions: filterCommandPaletteSessions(workspaces, query),
    actions: filterCommands(canRestartBackend ? [...ACTION_COMMANDS, RESTART_BACKEND_COMMAND] : ACTION_COMMANDS, terms),
    settings: filterCommands(SETTINGS_COMMANDS, terms),
  };
}
