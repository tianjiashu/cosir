import { describe, expect, it } from "vitest";

import { queryKeys } from "@/lib/query/query-keys";

/** 系统级写入按前缀失效全部作用域：每个作用域的列表与详情键都必须以对应前缀开头。 */
describe("skill / mcp query keys", () => {
  it("nest every scope and document under the all-scopes prefix", () => {
    for (const workspaceId of [null, 7]) {
      expect(queryKeys.skills(workspaceId).slice(0, 2)).toEqual([...queryKeys.skillsAll]);
      expect(queryKeys.skill(workspaceId, "x").slice(0, 3)).toEqual([...queryKeys.skills(workspaceId)]);
      expect(queryKeys.mcpServers(workspaceId).slice(0, 2)).toEqual([...queryKeys.mcpServersAll]);
    }
    expect(queryKeys.skills(7)).not.toEqual(queryKeys.skills(null));
  });
});
