/**
 * 工具展示渲染规则层单测。
 *
 * 覆盖：
 * - 9 个工具的请求摘要 / 结果投影（正常、缺字段、空数组）。
 * - `toToolDisplayHints` 非法输入降级与合法字段收窄。
 *
 * 设计：纯函数、无副作用、不抛异常；每个分支断言「输入 → 投影」的可观测结果。
 *
 * @module tests/toolDisplayRules
 */

import { describe, expect, it } from "vitest";
import {
  type ToolDataRecord,
  projectToolRequestSummary,
  projectToolResult,
} from "@shared/toolDisplayRules";
import {
  DEFAULT_TOOL_DISPLAY_HINTS,
  toToolDisplayHints,
  type ToolExpandLayout,
} from "@shared/toolDisplay";

describe("projectToolRequestSummary", () => {
  it("未登记工具返回空串", () => {
    expect(projectToolRequestSummary("no_such_tool")).toBe("");
  });

  it("read_file 缺参降级为空串路径", () => {
    expect(projectToolRequestSummary("read_file", {})).toBe(" L1-End");
  });

  it("read_file 正常投影行范围", () => {
    expect(projectToolRequestSummary("read_file", { path: "a/b.py", offset: 10, limit: 5 })).toBe(
      "a/b.py L10-14",
    );
  });

  it("write_file 缺参降级为字面量", () => {
    expect(projectToolRequestSummary("write_file", {})).toBe("write");
  });

  it("write_file 正常取路径", () => {
    expect(projectToolRequestSummary("write_file", { path: "x.ts" })).toBe("x.ts");
  });

  it("delete 递归标注", () => {
    expect(projectToolRequestSummary("delete", { path: "dir", recursive: true })).toBe("dir （递归）");
  });

  it("delete 非递归无标注", () => {
    expect(projectToolRequestSummary("delete", { path: "f.txt" })).toBe("f.txt");
  });

  it("list_directory 根目录中文", () => {
    expect(projectToolRequestSummary("list_directory", { path: "/" })).toBe("根目录");
  });

  it("search_files content 模式带 file_glob 时展示范围与 glob", () => {
    expect(
      projectToolRequestSummary("search_files", { pattern: "foo", path: "src", file_glob: "*.ts" }),
    ).toBe("foo in src (*.ts)");
  });

  it("search_files 默认相对路径展示 pattern", () => {
    expect(projectToolRequestSummary("search_files", { pattern: "foo", target: "content" })).toBe(
      "foo",
    );
  });

  it("search_files 绝对路径范围正常展示", () => {
    expect(
      projectToolRequestSummary("search_files", { pattern: "foo", path: "/abs/dir" }),
    ).toBe("foo in /abs/dir");
  });

  it("search_files files 模式忽略 file_glob（只展示范围）", () => {
    expect(
      projectToolRequestSummary("search_files", {
        pattern: "foo",
        path: "src",
        file_glob: "*.ts",
        target: "files",
      }),
    ).toBe("foo in src");
  });

  it("execute_terminal 空命令降级", () => {
    expect(projectToolRequestSummary("execute_terminal", { command: "   " })).toBe("（空命令）");
  });

  it("execute_terminal 正常取命令", () => {
    expect(projectToolRequestSummary("execute_terminal", { command: "ls -la" })).toBe("ls -la");
  });

  it("web_search 取 query", () => {
    expect(projectToolRequestSummary("web_search", { query: "rust async" })).toBe("rust async");
  });

  it("web_extract 展示 URL 数量", () => {
    expect(projectToolRequestSummary("web_extract", { urls: ["a", "b", "c"] })).toBe("3 URL(s)");
  });

  it("web_extract 非数组 urls 降级为 0", () => {
    expect(projectToolRequestSummary("web_extract", { urls: "not-array" })).toBe("0 URL(s)");
  });
});

describe("projectToolResult", () => {
  it("未登记工具返回空投影", () => {
    const p = projectToolResult("no_such_tool");
    expect(p).toEqual({ summary: null, listEntries: [], emptyLabel: null, diffEntries: [] });
  });

  it("read_file 永远空投影（折叠态正文走 result 文本）", () => {
    expect(projectToolResult("read_file", { content: "x" })).toEqual({
      summary: null,
      listEntries: [],
      emptyLabel: null,
      diffEntries: [],
    });
  });

  it("delete 投影被删路径为折叠态摘要（修复信息丢失缺陷）", () => {
    const p = projectToolResult("delete", { path: "/p/a.txt", path_basename: "a.txt" });
    expect(p.summary).toBe("/p/a.txt");
  });

  it("delete 缺 path 时返回 null（降级回 requestSummary，不覆盖有信息内容）", () => {
    const p = projectToolResult("delete", {});
    expect(p.summary).toBeNull();
  });

  it("delete 仅含 path_basename 时也能投影", () => {
    const p = projectToolResult("delete", { path_basename: "a.txt" });
    expect(p.summary).toBe("a.txt");
  });

  it("execute_terminal 空投影", () => {
    expect(projectToolResult("execute_terminal", { output: "x" })).toEqual({
      summary: null,
      listEntries: [],
      emptyLabel: null,
      diffEntries: [],
    });
  });

  it("write_file / patch 无 changes 时给空态摘要", () => {
    const p = projectToolResult("write_file", {});
    expect(p.summary).toBe("（没有文本变更）");
    expect(p.diffEntries).toEqual([]);
  });

  it("patch 正常投影 diff 条目", () => {
    const data: ToolDataRecord = {
      changes: [
        {
          path: "a.py",
          new_path: "",
          status: "modified",
          insertions: 3,
          deletions: 1,
        },
      ],
    };
    const p = projectToolResult("patch", data);
    expect(p.diffEntries).toEqual([
      {
        path: "a.py",
        name: "a.py",
        newPath: null,
        status: "modified",
        insertions: 3,
        deletions: 1,
      },
    ]);
  });

  it("list_directory 正常投影目录条目", () => {
    const data: ToolDataRecord = {
      entries: [{ name: "src", path: ".", type: "dir" }],
    };
    const p = projectToolResult("list_directory", data);
    expect(p.listEntries).toEqual([{ name: "src", path: ".", type: "dir" }]);
    expect(p.emptyLabel).toBeNull();
  });

  it("list_directory 空目录给空态", () => {
    const p = projectToolResult("list_directory", { entries: [] });
    expect(p.emptyLabel).toBe("（空目录）");
  });

  it("search_files files 模式投影文件名条目（item 为相对 searchPath 的路径）", () => {
    const data: ToolDataRecord = {
      items: ["a.ts", "sub/b.ts"],
      target: "files",
      path: "src",
    };
    const p = projectToolResult("search_files", data);
    expect(p.listEntries).toEqual([
      { name: "a.ts", path: "src", type: "file" },
      { name: "b.ts", path: "src/sub", type: "file" },
    ]);
  });

  it("search_files content 模式投影命中行（file_path 为相对 searchPath 的路径）", () => {
    const data: ToolDataRecord = {
      items: [{ file_path: "a.ts", line_number: 12, content: "const x = 1" }],
      target: "content",
      path: "src",
    };
    const p = projectToolResult("search_files", data);
    expect(p.listEntries[0]).toMatchObject({
      name: "a.ts",
      lineNumber: 12,
      content: "const x = 1",
    });
    expect(p.listEntries[0].filePath).toBe("src/a.ts");
  });

  it("search_files 无命中给空态文案", () => {
    const data: ToolDataRecord = { items: [], target: "content", path: "." };
    const p = projectToolResult("search_files", data);
    expect(p.emptyLabel).toBe("没有搜索到相关内容");
  });

  it("search_files 非数组 items 降级空数组", () => {
    const p = projectToolResult("search_files", { items: "bad", target: "content" });
    expect(p.listEntries).toEqual([]);
  });

  it("web_search 空结果给空态摘要", () => {
    const p = projectToolResult("web_search", { web: [] });
    expect(p.summary).toBe("（没有web结果）");
    expect(p.emptyLabel).toBe("（没有结果）");
  });

  it("web_search 正常投影列表与计数", () => {
    const data: ToolDataRecord = {
      web: [
        { title: "T1", url: "https://a.com" },
        { title: "", url: "https://b.com" },
      ],
    };
    const p = projectToolResult("web_search", data);
    expect(p.summary).toBe("2 web results");
    expect(p.listEntries).toHaveLength(2);
    // 缺 title 时回退到 url 作为 name
    expect(p.listEntries[1].name).toBe("https://b.com");
  });

  it("web_search 非数组 web 降级空数组", () => {
    const p = projectToolResult("web_search", { web: "bad" });
    expect(p.listEntries).toEqual([]);
  });

  it("web_extract 投影每个 URL 的正文为 list 条目（修复正文被 JSON 包裹丢弃缺陷）", () => {
    const data: ToolDataRecord = {
      web: [
        { title: "Page A", url: "https://x.com/a", content: "body of A" },
        { title: "", url: "https://x.com/b", content: "body of B" },
      ],
    };
    const p = projectToolResult("web_extract", data);
    expect(p.summary).toBe("2 extracted pages");
    expect(p.listEntries).toHaveLength(2);
    // 用 url 兜底 name，content 透传为正文预览
    expect(p.listEntries[0]).toMatchObject({
      name: "Page A",
      path: "https://x.com/a",
      content: "body of A",
    });
    expect(p.listEntries[1].name).toBe("https://x.com/b");
  });

  it("web_extract 透传逐项截断标记（content_truncated）", () => {
    const data: ToolDataRecord = {
      web: [{ title: "P", url: "https://x.com", content: "long", content_truncated: true }],
    };
    const p = projectToolResult("web_extract", data);
    expect(p.listEntries[0].contentTruncated).toBe(true);
  });

  it("web_extract 无截断标记时 contentTruncated 为 false", () => {
    const data: ToolDataRecord = {
      web: [{ title: "P", url: "https://x.com", content: "short" }],
    };
    const p = projectToolResult("web_extract", data);
    expect(p.listEntries[0].contentTruncated).toBe(false);
  });

  it("web_extract 无 content 时仍列 URL（web_search 风格降级）", () => {
    const data: ToolDataRecord = {
      web: [{ title: "T", url: "https://y.com" }],
    };
    const p = projectToolResult("web_extract", data);
    expect(p.listEntries[0].content).toBeUndefined();
  });

  it("search_files files 模式无命中给空数组与空态文案", () => {
    const p = projectToolResult("search_files", { items: [], target: "files", path: "src" });
    expect(p.listEntries).toEqual([]);
    expect(p.emptyLabel).toBe("没有搜索到相关内容");
  });

  it("patch rename 分支 new_path 非空时投影 newPath", () => {
    const data: ToolDataRecord = {
      changes: [
        { path: "old.py", new_path: "new.py", status: "moved", insertions: 0, deletions: 0 },
      ],
    };
    const p = projectToolResult("patch", data);
    expect(p.diffEntries[0]).toMatchObject({
      path: "old.py",
      name: "old.py",
      newPath: "new.py",
      status: "moved",
    });
  });

  it("list_directory 元素缺 name/type 时安全降级", () => {
    const data: ToolDataRecord = {
      entries: [{ path: "x" }, {}, null, "not-an-object"],
    };
    const p = projectToolResult("list_directory", data);
    // readRecordList 过滤非对象元素；缺 name 降级空串、缺 type 降级 file
    expect(p.listEntries).toEqual([
      { name: "", path: "x", type: "file" },
      { name: "", path: ".", type: "file" },
    ]);
  });

  it("readRecordList 过滤数组中的 null 与标量（仅留对象）", () => {
    const data: ToolDataRecord = {
      web: [null, "str", 42, { title: "ok", url: "https://a.com" }],
    };
    const p = projectToolResult("web_search", data);
    expect(p.listEntries).toHaveLength(1);
    expect(p.listEntries[0].name).toBe("ok");
  });

  it("search_files 绝对搜索根下 joinDisplayPath 不重复拼接", () => {
    const data: ToolDataRecord = {
      items: [{ file_path: "a.ts", line_number: 1, content: "x" }],
      target: "content",
      path: "/abs/src",
    };
    const p = projectToolResult("search_files", data);
    // 绝对路径 searchPath 时 filePath 直接取相对段，不再拼 searchPath
    expect(p.listEntries[0].filePath).toBe("a.ts");
  });
});

describe("toToolDisplayHints", () => {
  it("非对象输入返回默认声明", () => {
    expect(toToolDisplayHints(undefined)).toEqual(DEFAULT_TOOL_DISPLAY_HINTS);
    expect(toToolDisplayHints(null)).toEqual(DEFAULT_TOOL_DISPLAY_HINTS);
    expect(toToolDisplayHints("str")).toEqual(DEFAULT_TOOL_DISPLAY_HINTS);
    expect(toToolDisplayHints(42)).toEqual(DEFAULT_TOOL_DISPLAY_HINTS);
  });

  it("合法字段全部收窄", () => {
    const hints = toToolDisplayHints({
      verb: "读取",
      icon: "eye",
      expandable: false,
      expand_layout: "none",
    });
    expect(hints).toEqual({
      verb: "读取",
      icon: "eye",
      expandable: false,
      expandLayout: "none",
    });
  });

  it("非法 expand_layout 降级默认", () => {
    const hints = toToolDisplayHints({ expand_layout: "bogus" });
    expect(hints.expandLayout).toBe(DEFAULT_TOOL_DISPLAY_HINTS.expandLayout);
  });

  it("合法 expand_layout 集合全部接受", () => {
    const layouts: ToolExpandLayout[] = [
      "none",
      "details",
      "list",
      "diff",
      "write",
      "terminal",
    ];
    for (const layout of layouts) {
      const hints = toToolDisplayHints({ expand_layout: layout });
      expect(hints.expandLayout).toBe(layout);
    }
  });

  it("非布尔 expandable 降级默认", () => {
    const hints = toToolDisplayHints({ expandable: "yes" });
    expect(hints.expandable).toBe(DEFAULT_TOOL_DISPLAY_HINTS.expandable);
  });

  it("缺字段时逐项降级默认", () => {
    const hints = toToolDisplayHints({});
    expect(hints).toEqual(DEFAULT_TOOL_DISPLAY_HINTS);
  });
});
