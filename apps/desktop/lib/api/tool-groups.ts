import { requestJson, type RequestOptions } from "@/lib/http/client";

/** 主 Agent 可用工具的分组名，配置中心据此勾选子 Agent 允许的工具组。 */
export function getToolGroups(options?: RequestOptions) {
  return requestJson<{ groups: string[] }>("/tools/groups", options);
}
