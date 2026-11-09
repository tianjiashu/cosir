import { describe, expect, it } from "vitest";

import { parseCapsules, serializeCapsule } from "@/lib/assistant/capsules";
import {
  refDescription,
  refIsDirectory,
  refLineRange,
  refLocatorText,
  refName,
  refWorkspacePath,
  workspaceDirectoryRef,
  workspaceFileRef,
  workspacePageRef,
} from "@/lib/workspace-references/ref-capsule";

describe("工作区引用胶囊", () => {
  it("文件引用带行范围时与胶囊语法一致", () => {
    const capsule = workspaceFileRef("src/a.py", { start: 12, end: 14 });
    expect(capsule).toEqual({ kind: "ref", path: "src/a.py", lines: "12-14" });
    expect(serializeCapsule(capsule)).toBe('<ref path="src/a.py" lines="12-14" />');
    expect(refLineRange(capsule)).toEqual({ start: 12, end: 14 });
    expect(refName(capsule)).toBe("a.py");
    expect(refLocatorText(capsule)).toBe(" · L12–14");
    expect(refDescription(capsule)).toBe("src/a.py · L12–14");
  });

  it("单行范围收成单个数字；没有定位时只显示路径", () => {
    const oneLine = workspaceFileRef("README.md", { start: 7, end: 7 });
    expect(oneLine.lines).toBe("7");
    expect(serializeCapsule(oneLine)).toBe('<ref path="README.md" lines="7" />');
    expect(refLocatorText(oneLine)).toBe(" · L7");
    const wholeFile = workspaceFileRef("README.md");
    expect(refLineRange(wholeFile)).toBeNull();
    expect(refLocatorText(wholeFile)).toBeUndefined();
    expect(refDescription(wholeFile)).toBe("README.md");
  });

  it("目录引用统一以 `/` 结尾，根目录用空路径（`/` 是绝对路径）", () => {
    expect(workspaceDirectoryRef("src")).toEqual({ kind: "ref", path: "src/" });
    expect(workspaceDirectoryRef("src/")).toEqual({ kind: "ref", path: "src/" });
    expect(workspaceDirectoryRef("")).toEqual({ kind: "ref", path: "" });
    expect(refIsDirectory(workspaceDirectoryRef("src"))).toBe(true);
    expect(refIsDirectory(workspaceDirectoryRef(""))).toBe(true);
    expect(refWorkspacePath(workspaceDirectoryRef("src"))).toBe("src");
    expect(refWorkspacePath(workspaceDirectoryRef(""))).toBe("");
    expect(refName(workspaceDirectoryRef("src"))).toBe("src/");
    expect(refName(workspaceDirectoryRef(""))).toBe("工作区");
  });

  it("PDF 引用用页码定位，并能被胶囊语法解析回来", () => {
    const capsule = workspacePageRef("docs/guide.pdf", 3);
    expect(serializeCapsule(capsule)).toBe('<ref path="docs/guide.pdf" page="3" />');
    expect(refLocatorText(capsule)).toBe(" · p.3");
    expect(parseCapsules(serializeCapsule(capsule))).toEqual([
      { type: "capsule", capsule },
    ]);
  });
});
