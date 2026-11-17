import { afterEach, describe, expect, it } from "vitest";

import { useFileWatchStore } from "@/lib/workspace-files/file-watch-store";

const registered = () => useFileWatchStore.getState().byWorkspace;
afterEach(() => useFileWatchStore.setState({ byWorkspace: {} }));

// 目的：监听连接按工作区开合；潜在缺陷：一方撤销把另一方还在用的路径一起删掉，或登记清零后连接不关。
describe("useFileWatchStore", () => {
  it("同一路径按登记次数计，最后一方撤销才移除；工作区登记清零即消失", () => {
    const { register } = useFileWatchStore.getState();
    const panel = register(7, ["src", "docs"]);
    const session = register(7, ["src/a.png", "src"]);
    const other = register(9, ["x.md"]);
    expect(registered()).toEqual({ 7: { src: 2, docs: 1, "src/a.png": 1 }, 9: { "x.md": 1 } });
    panel();
    expect(registered()[7]).toEqual({ src: 1, "src/a.png": 1 });
    session();
    other();
    expect(registered()).toEqual({});
  });

  it("重复路径只登记一次，空集合不登记", () => {
    const { register } = useFileWatchStore.getState();
    const release = register(7, ["a", "a"]);
    expect(registered()[7]).toEqual({ a: 1 });
    release();
    register(7, [])();
    expect(registered()).toEqual({});
  });
});
