import { describe, expect, it } from "vitest";

import { inlineCodeLink, resolveLink, toWorkspacePath } from "@/lib/links/resolve-link";

const repo = { workspaceRoot: "/repo" };

describe("toWorkspacePath", () => {
  it("keeps workspace-relative paths and normalizes dot segments", () => {
    expect(toWorkspacePath("/repo", "src/app.ts")).toBe("src/app.ts");
    expect(toWorkspacePath("/repo", "./src/../lib/a.ts")).toBe("lib/a.ts");
  });

  it("maps absolute paths inside the workspace and rejects the rest", () => {
    expect(toWorkspacePath("/repo/", "/repo/src/app.ts")).toBe("src/app.ts");
    expect(toWorkspacePath("/repo", "/repository/a.ts")).toBeNull();
    expect(toWorkspacePath("/repo", "/etc/passwd")).toBeNull();
    expect(toWorkspacePath("/repo", "../outside.ts")).toBeNull();
    expect(toWorkspacePath("/repo", "  ")).toBeNull();
  });

  it("accepts Windows paths regardless of separator and drive case", () => {
    expect(toWorkspacePath("C:\\work\\repo", "c:\\work\\repo\\src\\app.ts")).toBe("src/app.ts");
  });
});

describe("resolveLink", () => {
  it("reads line numbers from the #L fragment and tolerates :line suffixes", () => {
    expect(resolveLink("src/app.ts#L42", repo)).toEqual({ kind: "workspace", path: "src/app.ts", directory: false, lines: { start: 42, end: 42 } });
    expect(resolveLink("src/app.ts#L42-L60", repo)).toMatchObject({ lines: { start: 42, end: 60 } });
    expect(resolveLink("src/app.ts#L42-60", repo)).toMatchObject({ lines: { start: 42, end: 60 } });
    expect(resolveLink("src/app.ts:42:7", repo)).toMatchObject({ path: "src/app.ts", lines: { start: 42, end: 42 } });
    expect(resolveLink("/repo/src/app.ts:3", repo)).toMatchObject({ path: "src/app.ts", lines: { start: 3, end: 3 } });
    expect(resolveLink("docs/guide.md#install", repo)).toEqual({ kind: "workspace", path: "docs/guide.md", directory: false });
  });

  it("decodes percent-encoded Markdown hrefs and recognizes directories", () => {
    expect(resolveLink("src/sp%20ace%E4%B8%AD.ts", repo)).toMatchObject({ path: "src/sp ace中.ts" });
    expect(resolveLink("apps/desktop/", repo)).toEqual({ kind: "workspace", path: "apps/desktop", directory: true });
  });

  it("opens reference URLs copied from user messages", () => {
    expect(resolveLink("cosir-file:src/a.ts#L3-L5", repo)).toEqual({ kind: "workspace", path: "src/a.ts", directory: false, lines: { start: 3, end: 5 } });
    expect(resolveLink("cosir-dir:src", repo)).toEqual({ kind: "workspace", path: "src", directory: true });
    expect(resolveLink("cosir-notes/a.md", repo)).toMatchObject({ path: "cosir-notes/a.md" });
  });

  it("classifies web links and marks local development servers", () => {
    expect(resolveLink("https://example.com/a", repo)).toEqual({ kind: "web", url: "https://example.com/a", local: false });
    expect(resolveLink("http://localhost:5173/", repo)).toMatchObject({ local: true });
    expect(resolveLink("http://127.0.0.1:8000", { workspaceRoot: null })).toMatchObject({ local: true });
    expect(resolveLink("http://app.localhost", repo)).toMatchObject({ local: true });
    expect(resolveLink("mailto:a@b.c", repo)).toMatchObject({ kind: "web", local: false });
  });

  it("rejects other protocols, anchors and paths outside the workspace", () => {
    for (const href of ["javascript:alert(1)", "file:///etc/passwd", "vscode://x", "#", "//cdn.example.com/a.js", "../outside.ts", "/etc/passwd"]) {
      expect(resolveLink(href, repo)).toBeNull();
    }
  });

  it("treats fragments as anchors inside the same document", () => {
    expect(resolveLink("#user-content-fn-1", repo)).toEqual({ kind: "anchor", id: "user-content-fn-1" });
    expect(resolveLink("#%E5%AE%89%E8%A3%85", { workspaceRoot: null })).toEqual({ kind: "anchor", id: "安装" });
  });

  it("only resolves web links without a workspace", () => {
    expect(resolveLink("src/app.ts", { workspaceRoot: null })).toBeNull();
    expect(resolveLink("cosir-file:src/a.ts", { workspaceRoot: null })).toBeNull();
  });

  it("resolves document links against the document directory, with / meaning the workspace root", () => {
    const doc = { workspaceRoot: "/repo", documentDir: "docs/plan" };
    expect(resolveLink("./b.md", doc)).toMatchObject({ path: "docs/plan/b.md" });
    expect(resolveLink("../a.md#L2", doc)).toMatchObject({ path: "docs/a.md", lines: { start: 2, end: 2 } });
    expect(resolveLink("/README.md", doc)).toMatchObject({ path: "README.md" });
    expect(resolveLink("/repo/src/a.ts", doc)).toMatchObject({ path: "src/a.ts" });
    expect(resolveLink("../../../x.md", doc)).toBeNull();
    expect(resolveLink("b.md", { workspaceRoot: "/repo", documentDir: "" })).toMatchObject({ path: "b.md" });
  });

  it("accepts Windows drive paths inside the workspace", () => {
    expect(resolveLink("C:\\repo\\src\\a.ts:9", { workspaceRoot: "C:\\repo" })).toMatchObject({ path: "src/a.ts", lines: { start: 9, end: 9 } });
  });
});

describe("inlineCodeLink", () => {
  it("recognizes file paths with optional line suffixes", () => {
    expect(inlineCodeLink("src/app.ts:42", "/repo")).toMatchObject({ path: "src/app.ts", lines: { start: 42, end: 42 } });
    expect(inlineCodeLink("src/app.ts:42:7", "/repo")).toMatchObject({ path: "src/app.ts" });
    expect(inlineCodeLink("README.md", "/repo")).toMatchObject({ path: "README.md" });
    expect(inlineCodeLink("/repo/apps/main.py", "/repo")).toMatchObject({ path: "apps/main.py" });
  });

  it("marks bare file names as mentions, not addresses", () => {
    // 行文里提到的 `task.ts` 不知道在哪个目录：根目录只是第一个候选，打开入口找不到时改为按文件名查找。
    expect(inlineCodeLink("task.ts", "/repo")).toMatchObject({ path: "task.ts", mention: true });
    expect(inlineCodeLink("src/task.ts", "/repo")?.mention).toBeUndefined();
  });

  it("ignores code that only looks path-like", () => {
    for (const text of ["obj.method", "text/plain", "/api/tasks", "https://example.com/a.ts", "npm run dev", ".gitignore"]) {
      expect(inlineCodeLink(text, "/repo")).toBeNull();
    }
    expect(inlineCodeLink("src/app.ts", null)).toBeNull();
  });
});
