import { describe, expect, it } from "vitest";

import { pathFromInlineCode, toWorkspacePath } from "@/lib/workspace-files/workspace-path";

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

describe("pathFromInlineCode", () => {
  it("recognizes file paths with optional line suffixes", () => {
    expect(pathFromInlineCode("src/app.ts:42")).toBe("src/app.ts");
    expect(pathFromInlineCode("src/app.ts:42:7")).toBe("src/app.ts");
    expect(pathFromInlineCode("README.md")).toBe("README.md");
    expect(pathFromInlineCode("/repo/apps/main.py")).toBe("/repo/apps/main.py");
  });

  it("ignores code that only looks path-like", () => {
    expect(pathFromInlineCode("obj.method")).toBeNull();
    expect(pathFromInlineCode("text/plain")).toBeNull();
    expect(pathFromInlineCode("/api/tasks")).toBeNull();
    expect(pathFromInlineCode("https://example.com/a.ts")).toBeNull();
    expect(pathFromInlineCode("npm run dev")).toBeNull();
    expect(pathFromInlineCode(".gitignore")).toBeNull();
  });
});
