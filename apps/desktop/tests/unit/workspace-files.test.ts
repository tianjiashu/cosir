import { beforeEach, describe, expect, it } from "vitest";
import {
  fileSession,
  parentsOf,
  useFilesStore,
} from "@/lib/workspace-files/store";
import { useWorkbenchStore } from "@/lib/workbench/store";

describe("workspace file sessions", () => {
  beforeEach(() => useFilesStore.setState({ sessions: {} }));
  it("替换临时预览，固定页签保持且不同工作区隔离", () => {
    const store = useFilesStore.getState();
    store.open(1, "main.py");
    store.open(1, "config.json");
    expect(fileSession(useFilesStore.getState().sessions, 1).tabs).toEqual([
      { path: "config.json", pinned: false },
    ]);
    store.open(1, "config.json", true);
    store.open(1, "main.py");
    store.open(2, "other.py");
    expect(
      fileSession(useFilesStore.getState().sessions, 1).tabs.map((t) => t.path),
    ).toEqual(["config.json", "main.py"]);
    store.position(1, "main.py", { offset: 100, scrollTop: 200 });
    store.close(1, "main.py");
    expect(fileSession(useFilesStore.getState().sessions, 1).active).toBe(
      "config.json",
    );
    expect(
      fileSession(useFilesStore.getState().sessions, 1).positions["main.py"],
    ).toBeUndefined();
    store.forget(1);
    expect(useFilesStore.getState().sessions[2].active).toBe("other.py");
  });
  it("阅读偏好跨关闭与重新打开保留，关闭工作区后释放", () => {
    const store = useFilesStore.getState();
    store.open(1, "README.md", true);
    store.preference(1, "README.md", { mode: "split", wrap: true });
    store.position(1, "README.md", { previewScrollTop: 180 });
    store.open(1, "main.py");
    expect(fileSession(useFilesStore.getState().sessions, 1).browsing).toBe(
      false,
    );
    expect(
      fileSession(useFilesStore.getState().sessions, 1).positions["README.md"]
        .previewScrollTop,
    ).toBe(180);
    store.close(1, "README.md");
    store.open(1, "README.md");
    expect(
      fileSession(useFilesStore.getState().sessions, 1).preferences[
        "README.md"
      ],
    ).toEqual({ mode: "split", wrap: true });
    store.forget(1);
    expect(
      fileSession(useFilesStore.getState().sessions, 1).preferences,
    ).toEqual({});
  });
  it("目录定位包含全部祖先", () =>
    expect(parentsOf("apps/backend/main.py")).toEqual([
      "",
      "apps",
      "apps/backend",
    ]));
  it("Workbench 文件入口不依赖 Agent 标签且不重复打开", () => {
    useWorkbenchStore.getState().openFiles(2);
    useWorkbenchStore.getState().openFiles(2);
    const session = useWorkbenchStore.getState().sessions[2];
    expect(session.tabs.filter((t) => t.kind === "files")).toHaveLength(1);
    expect(session.panelOpen).toBe(true);
  });
});
