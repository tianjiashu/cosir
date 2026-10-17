import { describe, expect, it } from "vitest";
import { fileTabLabel } from "@/lib/workspace-files/tab-label";

describe("文件标签最短可区分路径", () => {
  it("唯一文件只展示文件名", () => {
    expect(fileTabLabel("src/main.py", ["src/main.py", "README.md"])).toBe("main.py");
  });
  it("重复父目录时逐级补足", () => {
    const paths = ["app/src/index.ts", "test/src/index.ts", "other/index.ts"];
    expect(paths.map(path => fileTabLabel(path, paths))).toEqual(paths);
  });
  it("根目录文件与嵌套文件可区分", () => {
    const paths = ["index.ts", "src/index.ts"];
    expect(paths.map(path => fileTabLabel(path, paths))).toEqual(paths);
  });
});
