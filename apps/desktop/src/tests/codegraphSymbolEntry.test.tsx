// @vitest-environment happy-dom
/**
 * CodeGraph 符号条目渲染与点击测试（ToolCallCard list 布局）。
 *
 * 验证「结构化条目可读、可点击打开文件」的核心不变量：
 * 1. 展开后符号条目呈现符号名、类型、关系边与 `文件:行号` 位置。
 * 2. 提供 onOpenFile 时条目为按钮，点击回传文件路径。
 * 3. 未提供 onOpenFile 时不渲染按钮（不出现「看似可点实则不可点」）。
 * 4. 内容搜索命中（无 kind/edge、带 content）仍走原有渲染分支，未被回归破坏。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import type { ToolListEntry } from "@/services/timeline/projector";

const SYMBOL_ENTRIES: ToolListEntry[] = [
  {
    name: "_spawn",
    path: "apps/backend/app/codegraph/supervisor.py",
    type: "file",
    filePath: "apps/backend/app/codegraph/supervisor.py",
    lineNumber: 238,
    kind: "method",
  },
  {
    name: "__init__.py",
    path: "apps/backend/app/codegraph/__init__.py",
    type: "file",
    filePath: "apps/backend/app/codegraph/__init__.py",
    lineNumber: 1,
    kind: "file",
    edge: "import",
  },
];

/** 展开工具卡片：折叠行摘要由「verb + 结果摘要」拼成，按前缀模糊匹配后点击。 */
function expandCard(summaryFragment: string) {
  const trigger = screen.getByText((content) => content.includes(summaryFragment));
  fireEvent.click(trigger);
}

function renderCard(onOpenFile?: (path: string) => void) {
  return render(
    <ToolCallCard
      toolName="codegraph_callers"
      status="completed"
      result="**Callers of resolve_node_binary (2 found)**"
      resultSummary="2 symbols"
      display={{
        icon: "network",
        expandable: true,
        expandLayout: "list",
        verb: "代码关系查询",
      }}
      listEntries={SYMBOL_ENTRIES}
      onOpenFile={onOpenFile}
    />,
  );
}

describe("CodeGraph 符号条目", () => {
  it("展开后呈现符号名、类型、关系边与文件位置", () => {
    renderCard(vi.fn());
    expandCard("2 symbols");

    expect(screen.getByText("_spawn")).toBeTruthy();
    expect(screen.getByText("method")).toBeTruthy();
    expect(screen.getByText("import")).toBeTruthy();
    expect(screen.getByText("apps/backend/app/codegraph/supervisor.py:238")).toBeTruthy();
  });

  it("提供 onOpenFile 时点击条目回传文件路径（不含行号）", () => {
    const onOpenFile = vi.fn();
    renderCard(onOpenFile);
    expandCard("2 symbols");

    fireEvent.click(screen.getByTitle("打开 apps/backend/app/codegraph/supervisor.py"));

    expect(onOpenFile).toHaveBeenCalledTimes(1);
    expect(onOpenFile).toHaveBeenCalledWith("apps/backend/app/codegraph/supervisor.py");
  });

  it("未提供 onOpenFile 时条目不渲染为按钮", () => {
    renderCard(undefined);
    expandCard("2 symbols");

    expect(screen.getByText("_spawn")).toBeTruthy();
    expect(screen.queryByTitle("打开 apps/backend/app/codegraph/supervisor.py")).toBeNull();
  });

  it("内容搜索命中条目不受影响（无 kind/edge 时仍渲染命中行文本）", () => {
    render(
      <ToolCallCard
        toolName="search_files"
        status="completed"
        result="hit"
        resultSummary="1 match"
        display={{ icon: "search", expandable: true, expandLayout: "list", verb: "搜索" }}
        listEntries={[
          { name: "a.py", path: "src", filePath: "src/a.py", lineNumber: 7, content: "def a():" },
        ]}
        onOpenFile={vi.fn()}
      />,
    );
    expandCard("1 match");

    expect(screen.getByText("def a():")).toBeTruthy();
    expect(screen.queryByTitle("打开 src/a.py")).toBeNull();
  });
});
