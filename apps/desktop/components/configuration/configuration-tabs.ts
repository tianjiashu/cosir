import {
  BotIcon,
  CpuIcon,
  Code2Icon,
  FileTextIcon,
  FilterIcon,
  KeyRoundIcon,
  NetworkIcon,
  PuzzleIcon,
  SlidersHorizontalIcon,
  SunMoonIcon,
  type LucideIcon,
} from "lucide-react";

/**
 * 配置中心标签与路由的唯一登记处：路由（/configuration/:tab、/workspaces/:id/configuration/:tab）、
 * 页面左侧导航、页面头部面包屑与各处跳转都从这里取 id、名称与路径，新增标签只改这里和对应页面的面板映射。
 */
export type ConfigurationTab<Id extends string> = { id: Id; label: string; icon: LucideIcon };

export const SYSTEM_CONFIGURATION_TABS = [
  { id: "appearance", label: "外观", icon: SunMoonIcon },
  { id: "agents", label: "子 Agent", icon: SlidersHorizontalIcon },
  { id: "models", label: "模型", icon: CpuIcon },
  { id: "main-agent", label: "主 Agent 提示词", icon: BotIcon },
  { id: "instructions", label: "全局 AGENTS.md", icon: FileTextIcon },
  { id: "environment", label: "环境变量", icon: KeyRoundIcon },
  { id: "mcp", label: "MCP 服务", icon: NetworkIcon },
  { id: "skills", label: "Skills", icon: PuzzleIcon },
] as const satisfies readonly ConfigurationTab<string>[];

export type SystemConfigurationTabId = (typeof SYSTEM_CONFIGURATION_TABS)[number]["id"];

export const WORKSPACE_CONFIGURATION_TABS = [
  { id: "agents", label: "子 Agent", icon: Code2Icon },
  { id: "instructions", label: "AGENTS.md", icon: FileTextIcon },
  { id: "fileignore", label: ".fileignore", icon: FilterIcon },
  { id: "mcp", label: "MCP 服务", icon: NetworkIcon },
  { id: "skills", label: "Skills", icon: PuzzleIcon },
] as const satisfies readonly ConfigurationTab<string>[];

export type WorkspaceConfigurationTabId = (typeof WORKSPACE_CONFIGURATION_TABS)[number]["id"];

/** 配置中心的路径：`workspaceId` 为 null 是系统配置，否则是该工作区的配置；带 `tab` 时定位到标签。 */
export function configurationPath(workspaceId: number | null, tab?: string): string {
  const root = workspaceId === null ? "/configuration" : `/workspaces/${workspaceId}/configuration`;
  return tab === undefined ? root : `${root}/${tab}`;
}

export function skillDetailPath(workspaceId: number | null, name: string): string {
  return `${configurationPath(workspaceId, "skills")}/${encodeURIComponent(name)}`;
}

/** 由路由参数定位标签：Skill 详情页（`…/configuration/skills/:skillName`）属于 Skills 标签，路由与页头共用这一处。 */
export function resolveConfigurationTab<Id extends string>(
  tabs: readonly ConfigurationTab<Id>[],
  params: Record<string, string | undefined>,
): ConfigurationTab<Id> | undefined {
  const id = params.skillName === undefined ? params.tab : "skills";
  return tabs.find((tab) => tab.id === id);
}
