import {
  BotIcon,
  CpuIcon,
  Code2Icon,
  FileTextIcon,
  FilterIcon,
  KeyRoundIcon,
  NetworkIcon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  SunMoonIcon,
  UsersIcon,
  type LucideIcon,
} from "lucide-react";

/**
 * 配置中心标签的唯一登记处：路由参数（/settings/:tab、/workspaces/:id/settings/:tab）、
 * 页面左侧导航与页面头部面包屑都从这里取 id 与名称，新增标签只改这里和对应页面的面板映射。
 */
export type SettingsTab<Id extends string> = { id: Id; label: string; icon: LucideIcon };

export const SYSTEM_SETTINGS_TABS = [
  { id: "appearance", label: "外观", icon: SunMoonIcon },
  { id: "agents", label: "子 Agent", icon: SlidersHorizontalIcon },
  { id: "models", label: "模型", icon: CpuIcon },
  { id: "agent-teams", label: "Agent Team", icon: UsersIcon },
  { id: "main-agent", label: "主 Agent 提示词", icon: BotIcon },
  { id: "instructions", label: "全局 AGENTS.md", icon: FileTextIcon },
  { id: "environment", label: "环境变量", icon: KeyRoundIcon },
  { id: "terminal-denylist", label: "终端命令规则", icon: ShieldCheckIcon },
  { id: "mcp", label: "MCP 服务", icon: NetworkIcon },
  { id: "skills", label: "Skills", icon: SparklesIcon },
] as const satisfies readonly SettingsTab<string>[];

export type SystemSettingsTabId = (typeof SYSTEM_SETTINGS_TABS)[number]["id"];

export const WORKSPACE_SETTINGS_TABS = [
  { id: "agents", label: "子 Agent", icon: Code2Icon },
  { id: "agent-teams", label: "Agent Team", icon: UsersIcon },
  { id: "instructions", label: "AGENTS.md", icon: FileTextIcon },
  { id: "fileignore", label: ".fileignore", icon: FilterIcon },
] as const satisfies readonly SettingsTab<string>[];

export type WorkspaceSettingsTabId = (typeof WORKSPACE_SETTINGS_TABS)[number]["id"];

export function findSettingsTab<Id extends string>(
  tabs: readonly SettingsTab<Id>[],
  id: string | undefined,
): SettingsTab<Id> | undefined {
  return tabs.find((tab) => tab.id === id);
}
