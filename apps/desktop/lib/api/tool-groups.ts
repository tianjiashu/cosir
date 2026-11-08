import { requestJson, type RequestOptions } from "@/lib/http/client";

export type ToolGroupCatalog = {
  group: string;
  tools: { name: string; description: string }[];
};

export function getToolGroups(options?: RequestOptions) {
  return requestJson<{ groups: ToolGroupCatalog[] }>("/tools/groups", options);
}
