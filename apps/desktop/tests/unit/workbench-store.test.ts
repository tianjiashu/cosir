import { describe, expect, it } from "vitest";
import { useWorkbenchStore } from "@/lib/workbench/store";

describe("workbench store", () => {
  it("deduplicates child Agent tabs and isolates workspaces", () => {
    const store = useWorkbenchStore.getState();
    store.ensureWorkspace(7);
    store.openAgentTab({ workspaceId: 7, taskId: 21, title: "审查", role: "Reviewer" });
    store.openAgentTab({ workspaceId: 7, taskId: 21, title: "审查（更新）", role: "Reviewer" });
    expect(useWorkbenchStore.getState().tabs).toHaveLength(1);
    expect(useWorkbenchStore.getState().activeTabId).toBe("agent-task:21");
    expect(useWorkbenchStore.getState().tabs[0].title).toBe("审查（更新）");

    store.ensureWorkspace(8);
    expect(useWorkbenchStore.getState().tabs).toEqual([]);
    expect(useWorkbenchStore.getState().activeTabId).toBeNull();
    store.ensureWorkspace(null);
  });
});
