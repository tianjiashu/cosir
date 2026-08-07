/**
 * CodeGraph 结果投影测试（共享渲染层）。
 *
 * 验证「后端 result_parser 产出 → ToolListEntry」投影的核心不变量：
 * 1. 5 个结构化工具都命中规则并产出符号条目（explore 不命中，维持全文）。
 * 2. 后端降级为 raw 全文时不产出条目、也不给出「查无结果」误导文案。
 * 3. 缺字段、脏数据一律安全降级，不抛异常。
 *
 * 输入样例与后端 `tests/test_codegraph_result_parser.py` 的产出结构保持一致。
 */
import { describe, expect, it } from "vitest";
import { projectToolResult } from "@shared/toolDisplayRules";

/** 后端 result_parser 对 callers 真实输出的解析产物（结构与字段名与后端一致）。 */
const CALLERS_DATA = {
  tool: "codegraph_callers",
  query_params: { symbol: "resolve_node_binary" },
  codegraph: {
    tool: "codegraph_callers",
    items: [
      {
        name: "_spawn",
        kind: "method",
        filePath: "apps/backend/app/codegraph/supervisor.py",
        lineNumber: 238,
        edge: "",
        signature: "",
      },
      {
        name: "__init__.py",
        kind: "file",
        filePath: "apps/backend/app/codegraph/__init__.py",
        lineNumber: 1,
        edge: "import",
        signature: "",
      },
    ],
  },
};

describe("CodeGraph 结果投影", () => {
  it("callers 结构化数据投影为符号条目，保留 kind/edge/行号", () => {
    const projection = projectToolResult("codegraph_callers", CALLERS_DATA);

    expect(projection.summary).toBe("2 symbols");
    expect(projection.listEntries).toHaveLength(2);
    expect(projection.listEntries[0]).toEqual({
      name: "_spawn",
      path: "apps/backend/app/codegraph/supervisor.py",
      type: "file",
      filePath: "apps/backend/app/codegraph/supervisor.py",
      lineNumber: 238,
      kind: "method",
      edge: undefined,
    });
    expect(projection.listEntries[1].edge).toBe("import");
    expect(projection.diffEntries).toEqual([]);
  });

  it("单条结果使用单数名词，避免 1 symbols 的英文错误", () => {
    const projection = projectToolResult("codegraph_search", {
      codegraph: { items: [{ name: "x", kind: "function", filePath: "a.py", lineNumber: 1 }] },
    });
    expect(projection.summary).toBe("1 symbol");
  });

  it("5 个结构化工具全部命中规则", () => {
    const data = {
      codegraph: { items: [{ name: "x", kind: "function", filePath: "a.py", lineNumber: 2 }] },
    };
    for (const tool of [
      "codegraph_search",
      "codegraph_node",
      "codegraph_callers",
      "codegraph_callees",
      "codegraph_impact",
    ]) {
      expect(projectToolResult(tool, data).listEntries).toHaveLength(1);
    }
  });

  it("explore 不命中规则，返回空投影以维持全文展示", () => {
    const projection = projectToolResult("codegraph_explore", {
      codegraph: { tool: "codegraph_explore", raw: "**Exploration:** ..." },
    });
    expect(projection.listEntries).toEqual([]);
    expect(projection.summary).toBeNull();
  });

  it("后端降级为 raw 时不产出条目，也不给出空态误导文案", () => {
    const projection = projectToolResult("codegraph_search", {
      codegraph: { tool: "codegraph_search", raw: "No results from CodeGraph query." },
    });
    expect(projection.listEntries).toEqual([]);
    expect(projection.summary).toBeNull();
    expect(projection.emptyLabel).toBeNull();
  });

  it("缺失 codegraph 字段 / 空 data 安全降级", () => {
    expect(projectToolResult("codegraph_node", {}).listEntries).toEqual([]);
    expect(projectToolResult("codegraph_node", undefined).listEntries).toEqual([]);
    expect(projectToolResult("codegraph_node", { codegraph: null }).listEntries).toEqual([]);
    expect(projectToolResult("codegraph_node", { codegraph: [] }).listEntries).toEqual([]);
  });

  it("条目字段类型异常时降级为空值而非抛异常", () => {
    const projection = projectToolResult("codegraph_impact", {
      codegraph: { items: [{ name: 42, filePath: null, lineNumber: "x", kind: undefined }] },
    });
    expect(projection.listEntries).toHaveLength(1);
    expect(projection.listEntries[0]).toEqual({
      name: "",
      path: "",
      type: "file",
      filePath: undefined,
      lineNumber: undefined,
      kind: undefined,
      edge: undefined,
    });
  });

  it("后端剥离的索引降级提示随 projection.notice 透传，不被丢弃", () => {
    const projection = projectToolResult("codegraph_node", {
      codegraph: {
        tool: "codegraph_node",
        notice: "⚠️ 索引可能不是最新的，结果可能过时",
        items: [
          {
            name: "resolve_node_binary",
            kind: "function",
            filePath: "apps/backend/app/codegraph/supervisor.py",
            lineNumber: 120,
            edge: "",
            signature: "",
          },
        ],
      },
    });
    expect(projection.listEntries).toHaveLength(1);
    expect(projection.notice).toBe("⚠️ 索引可能不是最新的，结果可能过时");
  });

  it("无 notice 时 projection.notice 为 null，避免渲染脏提示条", () => {
    const projection = projectToolResult("codegraph_callers", CALLERS_DATA);
    expect(projection.notice).toBeNull();
  });

  it("后端降级为 raw 但带 notice 时，notice 仍透传、不产出条目", () => {
    const projection = projectToolResult("codegraph_search", {
      codegraph: {
        tool: "codegraph_search",
        notice: "⚠️ 索引可能不是最新的",
        raw: "No results from CodeGraph query.",
      },
    });
    expect(projection.listEntries).toEqual([]);
    expect(projection.summary).toBeNull();
    expect(projection.notice).toBe("⚠️ 索引可能不是最新的");
  });
});
