import { requestJson } from "@/lib/http/client";

export type ToolGroupCatalog = {
  group: string;
  tools: { name: string; description: string }[];
};

export function getToolGroups(options?: Pick<RequestInit, "signal">) {
  return requestJson<{ groups: ToolGroupCatalog[] }>("/tools/groups", options);
}

/** 将配置页选中的工具组展开为后端 Agent profile 使用的工具名称。 */
export function toolNamesForGroups(groupNames: Iterable<string>, groups: ToolGroupCatalog[]): string[] {
  const selectedGroups = new Set(groupNames);
  const names: string[] = [];
  const seen = new Set<string>();
  for (const { group, tools } of groups) {
    if (!selectedGroups.has(group)) continue;
    for (const { name } of tools) {
      if (seen.has(name)) continue;
      names.push(name);
      seen.add(name);
    }
  }
  return names;
}

/** 将既有工具名称映射为“完整选中”的工具组，供多选框初始化使用。 */
export function toolGroupsForNames(toolNames: Iterable<string>, groups: ToolGroupCatalog[]): string[] {
  const selectedTools = new Set(toolNames);
  return groups
    .filter(({ tools }) => tools.every(({ name }) => selectedTools.has(name)))
    .map(({ group }) => group);
}
