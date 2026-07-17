/**
 * useWorkspaceStore 纯逻辑单测。
 *
 * 覆盖本次交付相关的 workspace 状态流转：
 * - 批量设置、新增/替换、删除、选中、折叠切换
 * - 边界：空列表、删除不存在的 id、toggle 不存在的 id、并发 upsert
 *
 * 注意：workspaceStore 仅 `import type { WorkspaceRecord } from "@shared/workspace"`（类型导入，运行时安全），
 * 因此本测试可直接导入真实 store，无需 mock @shared。
 *
 * @module tests/workspaceStore
 */

import { describe, it, expect, beforeEach } from "vitest";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import type { WorkspaceRecord } from "@shared/workspace";

function makeWorkspace(id: string, name = `ws-${id}`, path = `/path/${id}`): WorkspaceRecord {
  const now = new Date().toISOString();
  return {
    workspace_id: id,
    name,
    root_path: path,
    created_at: now,
    updated_at: now,
  };
}

describe("useWorkspaceStore — 基础状态流转", () => {
  beforeEach(() => {
    useWorkspaceStore.setState({
      workspaces: [],
      activeWorkspaceId: null,
      collapsedWorkspaceIds: new Set<string>(),
    });
  });

  it("setWorkspaces 空列表时 activeWorkspaceId 保持 null", () => {
    useWorkspaceStore.getState().setWorkspaces([]);
    const s = useWorkspaceStore.getState();
    expect(s.workspaces).toEqual([]);
    expect(s.activeWorkspaceId).toBeNull();
  });

  it("setWorkspaces 带数据时，当原 active 为空则选中第一个", () => {
    const list = [makeWorkspace("a"), makeWorkspace("b")];
    useWorkspaceStore.getState().setWorkspaces(list);
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("a");
  });

  it("setWorkspaces 保留已存在的 activeWorkspaceId", () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "b" });
    useWorkspaceStore.getState().setWorkspaces([makeWorkspace("a"), makeWorkspace("b")]);
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("b");
  });

  it("upsertWorkspace 新增工作区并置顶", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a"));
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("b"));
    const s = useWorkspaceStore.getState();
    expect(s.workspaces.map((w) => w.workspace_id)).toEqual(["b", "a"]);
  });

  it("upsertWorkspace 替换同名工作区而非重复添加", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a", "old"));
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a", "new"));
    const s = useWorkspaceStore.getState();
    expect(s.workspaces).toHaveLength(1);
    expect(s.workspaces[0].name).toBe("new");
  });

  it("upsertWorkspace 当无 active 时自动选中新工作区", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a"));
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("a");
  });

  it("upsertWorkspace 当有 active 时不覆盖已有选中", () => {
    useWorkspaceStore.setState({ activeWorkspaceId: "a" });
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("b"));
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("a");
  });

  it("removeWorkspace 删除存在的工作区", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a"));
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("b"));
    useWorkspaceStore.getState().removeWorkspace("a");
    expect(useWorkspaceStore.getState().workspaces.map((w) => w.workspace_id)).toEqual(["b"]);
  });

  it("removeWorkspace 删除当前 active 时回退为 null", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a"));
    useWorkspaceStore.setState({ activeWorkspaceId: "a" });
    useWorkspaceStore.getState().removeWorkspace("a");
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBeNull();
  });

  it("removeWorkspace 删除非 active 时不影响 active", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a"));
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("b"));
    useWorkspaceStore.setState({ activeWorkspaceId: "b" });
    useWorkspaceStore.getState().removeWorkspace("a");
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("b");
  });

  it("setActiveWorkspace 可设为有效 id 或 null", () => {
    useWorkspaceStore.getState().setActiveWorkspace("a");
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("a");
    useWorkspaceStore.getState().setActiveWorkspace(null);
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBeNull();
  });

  it("toggleWorkspaceCollapsed 首次切换为折叠，再次切换为展开", () => {
    useWorkspaceStore.getState().toggleWorkspaceCollapsed("a");
    expect(useWorkspaceStore.getState().collapsedWorkspaceIds.has("a")).toBe(true);
    useWorkspaceStore.getState().toggleWorkspaceCollapsed("a");
    expect(useWorkspaceStore.getState().collapsedWorkspaceIds.has("a")).toBe(false);
  });

  it("toggleWorkspaceCollapsed 对不存在的 id 创建折叠条目", () => {
    useWorkspaceStore.getState().toggleWorkspaceCollapsed("z");
    expect(useWorkspaceStore.getState().collapsedWorkspaceIds.has("z")).toBe(true);
  });
});

describe("useWorkspaceStore — 边界与异常", () => {
  beforeEach(() => {
    useWorkspaceStore.setState({
      workspaces: [],
      activeWorkspaceId: null,
      collapsedWorkspaceIds: new Set<string>(),
    });
  });

  it("removeWorkspace 删除不存在的 id 不产生错误且不改变列表", () => {
    useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a"));
    expect(() => useWorkspaceStore.getState().removeWorkspace("missing")).not.toThrow();
    expect(useWorkspaceStore.getState().workspaces).toHaveLength(1);
  });

  it("setActiveWorkspace 设为不存在的 id 不会报错（UI 层负责校验）", () => {
    expect(() => useWorkspaceStore.getState().setActiveWorkspace("ghost")).not.toThrow();
    expect(useWorkspaceStore.getState().activeWorkspaceId).toBe("ghost");
  });

  it("并发多次 upsert 同一 id 不产生重复条目", async () => {
    await Promise.all([
      useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a")),
      useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a")),
      useWorkspaceStore.getState().upsertWorkspace(makeWorkspace("a")),
    ]);
    expect(useWorkspaceStore.getState().workspaces).toHaveLength(1);
  });

  it("空路径工作区记录仍可被正常管理（边界数据）", () => {
    const ws = makeWorkspace("a", "empty", "");
    expect(() => useWorkspaceStore.getState().upsertWorkspace(ws)).not.toThrow();
    expect(useWorkspaceStore.getState().workspaces[0].root_path).toBe("");
  });
});
