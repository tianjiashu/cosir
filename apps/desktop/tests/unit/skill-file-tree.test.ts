import { describe, expect, it } from "vitest";

import { ancestorDirectories, buildSkillFileTree, type SkillTreeNode } from "@/lib/skills/skill-file-tree";

const file = (path: string, binary = false) => ({ path, size: 10, binary });

/** 树的紧凑形态：文件夹写成「名称(文件数)[子节点]」，文件只写名称。 */
function shape(nodes: SkillTreeNode[]): unknown[] {
  return nodes.map((node) => (node.kind === "directory" ? { [`${node.name}(${node.fileCount})`]: shape(node.children) } : node.name));
}

describe("skill file tree", () => {
  it("keeps SKILL.md first, folders before files, and counts nested files", () => {
    const tree = buildSkillFileTree([
      file("SKILL.md"),
      file("README.md"),
      file("scripts/build.py"),
      file("scripts/helpers/fmt.py"),
      file("assets/logo.png", true),
    ]);

    expect(shape(tree)).toEqual([
      "SKILL.md",
      { "assets(1)": ["logo.png"] },
      { "scripts(2)": [{ "helpers(1)": ["fmt.py"] }, "build.py"] },
      "README.md",
    ]);
  });

  it("shows intermediate folders that hold no files of their own", () => {
    // 以前的平铺清单只在「文件所在那一层」变化时显示标题，a/ 这一层会丢。
    expect(shape(buildSkillFileTree([file("SKILL.md"), file("a/b/c.md")]))).toEqual([
      "SKILL.md",
      { "a(1)": [{ "b(1)": ["c.md"] }] },
    ]);
  });

  it("lists the folders to expand to reach a file", () => {
    expect(ancestorDirectories("SKILL.md")).toEqual([]);
    expect(ancestorDirectories("a/b/c.md")).toEqual(["a", "a/b"]);
  });
});
