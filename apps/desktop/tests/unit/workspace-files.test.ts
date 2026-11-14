import { beforeEach, describe, expect, it } from "vitest";
import {
  workspaceFiles,
  parentsOf,
  useFilesStore,
} from "@/lib/workspace-files/workspace-files-store";
import { useDockStore } from "@/lib/layout/dock-store";

describe("workspace file sessions", () => {
  beforeEach(() => useFilesStore.setState({ byWorkspace: {} }));
  it("替换临时预览，固定页签保持且不同工作区隔离", () => {
    const store = useFilesStore.getState();
    store.open(1, "main.py");
    store.open(1, "config.json");
    expect(workspaceFiles(useFilesStore.getState().byWorkspace, 1).tabs).toEqual([
      { path: "config.json", pinned: false },
    ]);
    store.open(1, "config.json", true);
    store.open(1, "main.py");
    store.open(2, "other.py");
    expect(
      workspaceFiles(useFilesStore.getState().byWorkspace, 1).tabs.map((t) => t.path),
    ).toEqual(["config.json", "main.py"]);
    store.position(1, "main.py", { offset: 100, scrollTop: 200 });
    store.close(1, "main.py");
    expect(workspaceFiles(useFilesStore.getState().byWorkspace, 1).active).toBe(
      "config.json",
    );
    expect(
      workspaceFiles(useFilesStore.getState().byWorkspace, 1).positions["main.py"],
    ).toBeUndefined();
    store.forget(1);
    expect(useFilesStore.getState().byWorkspace[2].active).toBe("other.py");
  });
  it("阅读偏好跨关闭与重新打开保留，关闭工作区后释放", () => {
    const store = useFilesStore.getState();
    store.open(1, "README.md", true);
    store.preference(1, "README.md", { view: "source", wrap: true });
    store.position(1, "README.md", { previewScrollTop: 180 });
    store.open(1, "main.py");
    expect(workspaceFiles(useFilesStore.getState().byWorkspace, 1).browsing).toBe(
      false,
    );
    expect(
      workspaceFiles(useFilesStore.getState().byWorkspace, 1).positions["README.md"]
        .previewScrollTop,
    ).toBe(180);
    store.close(1, "README.md");
    store.open(1, "README.md");
    expect(
      workspaceFiles(useFilesStore.getState().byWorkspace, 1).preferences[
        "README.md"
      ],
    ).toEqual({ view: "source", wrap: true });
    store.forget(1);
    expect(
      workspaceFiles(useFilesStore.getState().byWorkspace, 1).preferences,
    ).toEqual({});
  });
  it("目录定位包含全部祖先", () =>
    expect(parentsOf("apps/backend/main.py")).toEqual([
      "",
      "apps",
      "apps/backend",
    ]));
  it("文件是独立面板：打开文件入口只切换停靠区，不进工作台标签", () => {
    useDockStore.getState().close();
    useDockStore.getState().toggle("files");
    expect(useDockStore.getState().panel).toBe("files");
    useDockStore.getState().toggle("files");
    expect(useDockStore.getState().panel).toBeNull();
  });
});
