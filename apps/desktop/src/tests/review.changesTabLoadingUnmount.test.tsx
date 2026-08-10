// @vitest-environment happy-dom
/**
 * 缺陷验证 #10：ChangesPanel 刷新时 loading 与文件列表互斥渲染，列表被卸载闪烁。
 *
 * 背景：ChangesTab.tsx 约 104-116 行：
 *   {loading && <p>加载中…</p>}
 *   {!loading && files.length > 0 && <VirtualList ... />}
 * useChanges.refresh() 每次都会 setLoading(true)（含 file_change_stable 校准、
 * file_change_updated 去抖刷新等后台增量刷新）。refresh 期间 loading=true →
 * 已有文件列表被整体卸载、替换为「加载中…」，refresh 结束再重新挂载——
 * 列表闪烁、滚动位置与行内交互状态丢失。正确行为：已有数据时增量刷新
 * 不应卸载列表（加载指示可叠加展示或仅首屏展示）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const hookState = vi.hoisted(() => ({
  current: null as Record<string, unknown> | null,
}));

// useChanges 替换为可控桩：测试直接驱动 loading/changeSet 状态组合。
vi.mock("@/hooks/useChanges", () => ({
  useChanges: () => hookState.current,
}));
// VirtualList 虚拟化与本缺陷无关，替换为全量渲染桩以便断言行是否挂载。
vi.mock("@/lib/virtual/VirtualList", () => ({
  VirtualList: ({ items, renderItem }: {
    items: Array<unknown>;
    renderItem: (item: unknown, index: number) => React.ReactNode;
  }) => <div data-testid="files-list">{items.map((item, i) => renderItem(item, i))}</div>,
}));
// 行组件与子控件替换为轻量桩，聚焦「列表挂载与否」这一断言点。
vi.mock("@/components/right-panel/ChangeFileRow", () => ({
  ChangeFileRow: ({ file }: { file: { path: string } }) => (
    <div data-testid="file-row">{file.path}</div>
  ),
}));
vi.mock("@/components/right-panel/ChangeCheckpointSelect", () => ({
  ChangeCheckpointSelect: () => null,
}));
vi.mock("@/components/right-panel/ChangesToolbar", () => ({
  ChangesToolbar: () => null,
}));

import { ChangesPanel } from "@/components/right-panel/ChangesTab";

function makeHookState(loading: boolean) {
  return {
    changeSet: {
      task_id: "task-1",
      checkpoints: [],
      files: [
        { path: "src/a.ts", action: "modified", status: "pending", last_tool_call_id: "", last_turn_id: "turn-1", additions: 3, deletions: 1 },
        { path: "src/b.ts", action: "added", status: "pending", last_tool_call_id: "", last_turn_id: "turn-1", additions: 10, deletions: 0 },
      ],
    },
    checkpoint: null,
    setCheckpoint: vi.fn(),
    revert: vi.fn(),
    keep: vi.fn(),
    refresh: vi.fn(),
    loading,
    error: null,
  };
}

describe("ChangesPanel 刷新期间的列表存活", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hookState.current = makeHookState(false);
  });

  // 测试目的：已有文件数据时进入 refresh（loading=true），文件列表应保持挂载。
  // 可能发现的缺陷：loading 与列表互斥渲染，后台增量刷新每次都卸载列表 →
  //   列表闪烁、滚动位置丢失。
  it("已有文件列表时 refresh（loading=true）不应卸载文件列表", () => {
    const { rerender } = render(<ChangesPanel taskId="task-1" />);
    // 前置确认：非加载态下列表正常挂载。
    expect(screen.getAllByTestId("file-row")).toHaveLength(2);

    // 模拟后台增量刷新开始：数据仍在，仅 loading 翻转为 true。
    hookState.current = makeHookState(true);
    rerender(<ChangesPanel taskId="task-1" />);

    // 正确行为：增量刷新期间已有列表保持挂载（加载指示可叠加，不应互斥）。
    expect(screen.getAllByTestId("file-row")).toHaveLength(2);
    expect(screen.getByText("src/a.ts")).toBeTruthy();
    expect(screen.getByText("src/b.ts")).toBeTruthy();
  });

  // 测试目的：正向对照——loading 翻回 false 后列表仍完整展示（harness 状态驱动正确）。
  // 可能发现的缺陷：无（此用例应 PASS）。
  it("对照：refresh 完成（loading=false）后列表正常展示", () => {
    hookState.current = makeHookState(true);
    const { rerender } = render(<ChangesPanel taskId="task-1" />);

    hookState.current = makeHookState(false);
    rerender(<ChangesPanel taskId="task-1" />);
    expect(screen.getAllByTestId("file-row")).toHaveLength(2);
  });
});
