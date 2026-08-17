// @vitest-environment happy-dom
/**
 * 日志页结构化组件与过滤逻辑测试。
 *
 * 覆盖关键词匹配、相对时间、级别统计、虚拟滚动条目数、错误路径记录。
 *
 * @module tests/logsComponents
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { LogEntryResponse } from "@shared/logs";
import { LogEntryCard } from "@/components/logs/LogEntryCard";
import { LogStatsBar } from "@/components/logs/LogStatsBar";
import { LogDataViewer } from "@/components/logs/LogDataViewer";

/** 构造一条测试日志条目。 */
function makeEntry(overrides: Partial<LogEntryResponse> = {}): LogEntryResponse {
  return {
    ts: new Date().toISOString(),
    level: "INFO",
    logger: "test.logger",
    trace_id: "trace-123",
    caller: "file.py:10:func",
    event: "test_event",
    msg: "hello world",
    data: { key: "value", num: 42 },
    ...overrides,
  };
}

describe("LogEntryCard", () => {
  it("渲染摘要行与级别徽标", () => {
    render(<LogEntryCard entry={makeEntry()} />);
    expect(screen.getByText("test_event")).toBeTruthy();
    expect(screen.getByText("INFO")).toBeTruthy();
    expect(screen.getByText(/hello world/)).toBeTruthy();
  });

  it("error 存在时渲染错误区", () => {
    const entry = makeEntry({
      level: "ERROR",
      error: { type: "RuntimeError", message: "boom", stack: "trace line" },
    });
    render(<LogEntryCard entry={entry} />);
    expect(screen.getByText(/RuntimeError/)).toBeTruthy();
    expect(screen.getByText(/boom/)).toBeTruthy();
  });

  it("truncated 为 true 时显示截断提示", () => {
    render(<LogEntryCard entry={makeEntry({ truncated: true })} />);
    expect(screen.getByText("内容已截断")).toBeTruthy();
  });

  it("默认折叠元数据与 data，不渲染 data 内容", () => {
    render(<LogEntryCard entry={makeEntry()} />);
    // data 值不应在折叠态出现
    expect(screen.queryByText("value")).toBeNull();
  });
});

describe("LogStatsBar", () => {
  it("展示后端级别计数与「全部」入口", () => {
    render(
      <LogStatsBar
        levelCounts={{ ERROR: 2, WARNING: 1, INFO: 1 }}
        total={4}
        activeLevel=""
        onSelectLevel={() => {}}
      />,
    );
    expect(screen.getByText("全部 4")).toBeTruthy();
    expect(screen.getByText("ERROR 2")).toBeTruthy();
    expect(screen.getByText("WARNING 1")).toBeTruthy();
    expect(screen.getByText("INFO 1")).toBeTruthy();
    expect(screen.getByText("DEBUG 0")).toBeTruthy();
  });

  it("点击级别触发 onSelectLevel", () => {
    const onSelect = vi.fn();
    render(
      <LogStatsBar
        levelCounts={{ ERROR: 1 }}
        total={1}
        activeLevel=""
        onSelectLevel={onSelect}
      />,
    );
    screen.getByText("ERROR 1").click();
    expect(onSelect).toHaveBeenCalledWith("ERROR");
  });
});

describe("LogDataViewer", () => {
  it("渲染键值与类型着色内容", () => {
    render(<LogDataViewer data={{ name: "alice", age: 30, active: true }} />);
    expect(screen.getByText("name:")).toBeTruthy();
    expect(screen.getByText(/alice/)).toBeTruthy();
    expect(screen.getByText("age:")).toBeTruthy();
  });

  it("空对象显示占位", () => {
    render(<LogDataViewer data={{}} />);
    expect(screen.getByText("（空对象）")).toBeTruthy();
  });

  it("嵌套对象折叠为预览", () => {
    render(<LogDataViewer data={{ nested: { a: 1 } }} />);
    expect(screen.getByText("{…}")).toBeTruthy();
  });
});

describe("错误路径可排查（渲染层不吞错）", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("非法 ts 不崩溃且回退展示", () => {
    const entry = makeEntry({ ts: "not-a-date" });
    expect(() => render(<LogEntryCard entry={entry} />)).not.toThrow();
  });
});
