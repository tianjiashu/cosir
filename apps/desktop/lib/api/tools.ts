import { requestJson } from "@/lib/http/client";

export type ToolGroupCatalog = {
  group: string;
  tools: { name: string; description: string }[];
};

export function getToolGroups(options?: Pick<RequestInit, "signal">) {
  return requestJson<{ groups: ToolGroupCatalog[] }>("/tools/groups", options);
}
