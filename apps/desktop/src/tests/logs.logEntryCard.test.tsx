// @vitest-environment happy-dom
/**
 * LogEntryCard 单元测试。
 *
 * 覆盖：级别色标/圆点/徽标变体映射、摘要行内容、元数据折叠、data 折叠展开、
 * error 区与 stack 折叠展开、截断提示、相对时间 Tooltip、以及非法/缺省输入边界。
 *
 * @module tests/logs.logEntryCard
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import type { LogEntryResponse } from "@shared/logs";
import { LogEntryCard } from "@/components/logs/LogEntryCard";

/** 固定"当前时间"基准，保证相对时间断言确定性。 */
const NOW = Date.parse("2024-05-20T12:00:00.000Z");

/** 构造一条测试日志条目。 */
function makeEntry(overrides: Partial<LogEntryResponse> = {}): LogEntryResponse {
  return {
    ts: "2024-05-20T11:58:00.000Z",
    level: "INFO",
    logger: "app.test",
    trace_id: "trace-abc",
    caller: "mod.py:12:fn",
    event: "test_event",
    msg: "hello world",
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("LogEntryCard 级别视觉映射", () => {
  // 测试目的：ERROR 级别左侧色标/圆点类名正确。可能发现的缺陷：级别→样式映射表错配或键名拼写错误。
  it("ERROR 渲染 border-l-red-500 与 bg-red-500 圆点", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "ERROR" })} />);
    const article = container.querySelector("article");
    expect(article?.className).toContain("border-l-red-500");
    expect(container.querySelector(".bg-red-500")).not.toBeNull();
  });

  // 测试目的：WARNING 级别色标为 amber。可能发现的缺陷：WARNING 与 ERROR 样式混用导致级别不可辨识。
  it("WARNING 渲染 border-l-amber-500 与 bg-amber-500 圆点", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "WARNING" })} />);
    expect(container.querySelector("article")?.className).toContain("border-l-amber-500");
    expect(container.querySelector(".bg-amber-500")).not.toBeNull();
  });

  // 测试目的：INFO 级别色标为 sky。可能发现的缺陷：默认兜底样式覆盖了 INFO 的专属配色。
  it("INFO 渲染 border-l-sky-500 与 bg-sky-500 圆点", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "INFO" })} />);
    expect(container.querySelector("article")?.className).toContain("border-l-sky-500");
    expect(container.querySelector(".bg-sky-500")).not.toBeNull();
  });

  // 测试目的：DEBUG 级别色标为 slate。可能发现的缺陷：DEBUG 缺失映射导致 undefined 类名注入。
  it("DEBUG 渲染 border-l-slate-400 与 bg-slate-400 圆点", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "DEBUG" })} />);
    expect(container.querySelector("article")?.className).toContain("border-l-slate-400");
    expect(container.querySelector(".bg-slate-400")).not.toBeNull();
  });

  // 测试目的：CRITICAL 使用更深红色以区别 ERROR。可能发现的缺陷：CRITICAL 未单独映射被兜底成灰色。
  it("CRITICAL 渲染 border-l-red-700 与 bg-red-700 圆点", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "CRITICAL" })} />);
    expect(container.querySelector("article")?.className).toContain("border-l-red-700");
    expect(container.querySelector(".bg-red-700")).not.toBeNull();
  });

  // 测试目的：未知级别兜底到 DEBUG 灰且不产生 "undefined" 类名。可能发现的缺陷：缺少 ?? 兜底导致读取 undefined 属性崩溃。
  it("未知级别兜底为 DEBUG 灰且类名不含 undefined", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "TRACE_X" })} />);
    const article = container.querySelector("article");
    expect(article?.className).toContain("border-l-slate-400");
    expect(article?.className).not.toContain("undefined");
    expect(container.innerHTML).not.toContain("undefined");
  });

  // 测试目的：ERROR 级别徽标使用 destructive 变体底色。可能发现的缺陷：badge 变体字符串未被 Badge 识别而回退 default。
  it("ERROR 徽标应用 bg-destructive 变体类", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "ERROR" })} />);
    expect(container.querySelector(".bg-destructive")).not.toBeNull();
  });

  // 测试目的：WARNING 徽标使用 warning 变体（amber-100 底）。可能发现的缺陷：Badge 未定义 warning 变体导致样式丢失。
  it("WARNING 徽标应用 bg-amber-100 变体类", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ level: "WARNING" })} />);
    expect(container.querySelector(".bg-amber-100")).not.toBeNull();
  });
});

describe("LogEntryCard 摘要行", () => {
  // 测试目的：摘要行同时含 event 与 msg。可能发现的缺陷：msg 被漏渲染或 event 被 msg 覆盖。
  it("摘要行包含 event 与 msg", () => {
    const { container } = render(
      <LogEntryCard entry={makeEntry({ event: "ev_a", msg: "msg_b" })} />,
    );
    expect(screen.getByText("ev_a")).toBeTruthy();
    expect(container.textContent).toContain("msg_b");
    expect(container.textContent).toContain("— msg_b");
  });

  // 测试目的：msg 为空串时不渲染多余分隔符。可能发现的缺陷：使用真值判断不当渲染出孤立 "—"。
  it("msg 为空时不渲染 '—' 分隔符", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ msg: "" })} />);
    expect(container.textContent).not.toContain("—");
    expect(screen.getByText("test_event")).toBeTruthy();
  });

  // 测试目的：级别文本常驻显示。可能发现的缺陷：徽标只渲染圆点丢失级别文字。
  it("徽标显示级别文本", () => {
    render(<LogEntryCard entry={makeEntry({ level: "ERROR" })} />);
    expect(screen.getByText("ERROR")).toBeTruthy();
  });
});

describe("LogEntryCard 相对时间", () => {
  // 测试目的：2 分钟前的 ts 显示"2分钟前"。可能发现的缺陷：秒/分换算错误或取整方向错误。
  it("2 分钟前显示 '2分钟前'", () => {
    render(<LogEntryCard entry={makeEntry({ ts: "2024-05-20T11:58:00.000Z" })} />);
    expect(screen.getByText("2分钟前")).toBeTruthy();
  });

  // 测试目的：不足 60 秒显示秒级。可能发现的缺陷：边界 <60 判断写成 <=60 或直接跳到分钟。
  it("30 秒前显示 '30秒前'", () => {
    render(<LogEntryCard entry={makeEntry({ ts: "2024-05-20T11:59:30.000Z" })} />);
    expect(screen.getByText("30秒前")).toBeTruthy();
  });

  // 测试目的：59 分钟仍为分钟级，60 分钟进位小时。可能发现的缺陷：分钟→小时边界 off-by-one。
  it("59 分钟显示分钟级、恰好 60 分钟进位为 1小时前", () => {
    const { unmount } = render(
      <LogEntryCard entry={makeEntry({ ts: "2024-05-20T11:01:00.000Z" })} />,
    );
    expect(screen.getByText("59分钟前")).toBeTruthy();
    unmount();
    render(<LogEntryCard entry={makeEntry({ ts: "2024-05-20T11:00:00.000Z" })} />);
    expect(screen.getByText("1小时前")).toBeTruthy();
  });

  // 测试目的：超过 24 小时显示天级。可能发现的缺陷：小时→天边界换算错误。
  it("48 小时前显示 '2天前'", () => {
    render(<LogEntryCard entry={makeEntry({ ts: "2024-05-18T12:00:00.000Z" })} />);
    expect(screen.getByText("2天前")).toBeTruthy();
  });

  // 测试目的：未来时间被夹到 0 秒而非负数。可能发现的缺陷：缺少 Math.max(0,…) 出现 "-60秒前"。
  it("未来时间戳夹为 '0秒前'（不出现负数）", () => {
    const { container } = render(
      <LogEntryCard entry={makeEntry({ ts: "2024-05-20T12:05:00.000Z" })} />,
    );
    expect(screen.getByText("0秒前")).toBeTruthy();
    expect(container.textContent).not.toContain("-");
  });

  // 测试目的：非法 ts 回退为原始字符串而不是 NaN。可能发现的缺陷：未做 NaN 判断输出 "NaN秒前"。
  it("非法 ts 回退显示原始字符串且不含 NaN", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ ts: "not-a-date" })} />);
    expect(screen.getByText("not-a-date")).toBeTruthy();
    expect(container.textContent).not.toContain("NaN");
  });

  // 测试目的：相对时间存在 Tooltip 触发器（可访问性挂载）。可能发现的缺陷：TooltipProvider 缺失导致 Radix 抛错或触发器未挂 aria。
  it("相对时间包裹 Tooltip 触发器（渲染不抛错且含 aria 描述属性）", () => {
    const { container } = render(<LogEntryCard entry={makeEntry()} />);
    const trigger = container.querySelector('[data-state]') as HTMLElement | null;
    expect(trigger).not.toBeNull();
    expect(screen.getByText("2分钟前")).toBeTruthy();
  });
});

describe("LogEntryCard 元数据折叠", () => {
  // 测试目的：元数据默认折叠且按钮 aria-expanded=false。可能发现的缺陷：初始 state 写成 true 或 aria 未同步。
  it("默认折叠：不渲染 trace_id/caller/logger 明细", () => {
    render(<LogEntryCard entry={makeEntry()} />);
    const btn = screen.getByText("展开元数据");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(/trace-abc/)).toBeNull();
  });

  // 测试目的：点击后展开出三项元数据并切换文案。可能发现的缺陷：toggle 未取反、文案未随状态变化。
  it("点击展开后显示 trace_id/caller/logger 且文案变为收起", () => {
    const { container } = render(<LogEntryCard entry={makeEntry()} />);
    fireEvent.click(screen.getByText("展开元数据"));
    expect(container.textContent).toContain("trace-abc");
    expect(container.textContent).toContain("mod.py:12:fn");
    expect(container.textContent).toContain("app.test");
    expect(screen.getByText("收起元数据").getAttribute("aria-expanded")).toBe("true");
  });

  // 测试目的：再次点击可折叠回去（状态可逆）。可能发现的缺陷：setState 使用非函数式写法导致只能单向展开。
  it("再次点击可折叠回去", () => {
    const { container } = render(<LogEntryCard entry={makeEntry()} />);
    fireEvent.click(screen.getByText("展开元数据"));
    fireEvent.click(screen.getByText("收起元数据"));
    expect(container.textContent).not.toContain("trace-abc");
  });

  // 测试目的：空元数据字段以 "—" 占位。可能发现的缺陷：空串直接渲染出空白，label 后无内容。
  it("trace_id/caller/logger 为空串时以 — 占位", () => {
    const { container } = render(
      <LogEntryCard entry={makeEntry({ trace_id: "", caller: "", logger: "" })} />,
    );
    fireEvent.click(screen.getByText("展开元数据"));
    const dashCount = (container.textContent ?? "").split("—").length - 1;
    expect(dashCount).toBeGreaterThanOrEqual(3);
  });
});

describe("LogEntryCard data 折叠面板", () => {
  // 测试目的：有 data 时默认折叠且显示字段数。可能发现的缺陷：字段计数用了错误对象或默认展开泄露长内容。
  it("有 data 时默认折叠并显示字段数", () => {
    render(<LogEntryCard entry={makeEntry({ data: { a: 1, b: "x" } })} />);
    expect(screen.getByText(/data（2 字段）/)).toBeTruthy();
    expect(screen.queryByText("a:")).toBeNull();
  });

  // 测试目的：点击展开渲染 data 内容（键与值）。可能发现的缺陷：LogDataViewer 未接线或 data 透传丢失。
  it("点击展开后渲染 data 键与值", () => {
    const { container } = render(
      <LogEntryCard entry={makeEntry({ data: { task_id: "t-1", count: 7 } })} />,
    );
    fireEvent.click(screen.getByText(/data（2 字段）/));
    expect(screen.getByText("task_id:")).toBeTruthy();
    expect(container.textContent).toContain("t-1");
    expect(container.textContent).toContain("7");
  });

  // 测试目的：无 data 字段时不渲染 data 面板。可能发现的缺陷：undefined 判断缺失渲染出空面板或崩溃。
  it("data 缺省时不渲染 data 面板", () => {
    render(<LogEntryCard entry={makeEntry({ data: undefined })} />);
    expect(screen.queryByText(/data（/)).toBeNull();
  });

  // 测试目的：data 为空对象时不渲染面板。可能发现的缺陷：仅判断存在性未判断 keys 长度，渲染 "data（0 字段）"。
  it("data 为空对象时不渲染 data 面板", () => {
    render(<LogEntryCard entry={makeEntry({ data: {} })} />);
    expect(screen.queryByText(/data（/)).toBeNull();
  });

  // 测试目的：data 展开可再次折叠。可能发现的缺陷：dataOpen 状态与 metaOpen 串用导致互相干扰。
  it("data 展开后可再次折叠且与元数据状态互不干扰", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ data: { k: "vvv" } })} />);
    fireEvent.click(screen.getByText(/data（1 字段）/));
    expect(container.textContent).toContain("vvv");
    // 展开元数据不应影响 data 展开态
    fireEvent.click(screen.getByText("展开元数据"));
    expect(container.textContent).toContain("vvv");
    expect(container.textContent).toContain("trace-abc");
    fireEvent.click(screen.getByText(/data（1 字段）/));
    expect(container.textContent).not.toContain("vvv");
    expect(container.textContent).toContain("trace-abc");
  });
});

describe("LogEntryCard error 区", () => {
  // 测试目的：error 存在时渲染 type 与 message 并使用 destructive 文本色。可能发现的缺陷：error 区未高亮或字段错位。
  it("渲染 error type 与 message 并带 text-destructive 高亮", () => {
    const { container } = render(
      <LogEntryCard
        entry={makeEntry({
          level: "ERROR",
          error: { type: "ValueError", message: "bad input" },
        })}
      />,
    );
    expect(container.textContent).toContain("ValueError");
    expect(container.textContent).toContain("bad input");
    expect(container.querySelector(".text-destructive")).not.toBeNull();
  });

  // 测试目的：stack 默认折叠。可能发现的缺陷：stackOpen 初值为 true 导致长堆栈直接铺开。
  it("stack 默认折叠不渲染堆栈内容", () => {
    const { container } = render(
      <LogEntryCard
        entry={makeEntry({
          error: { type: "E", message: "m", stack: "STACK_LINE_MARKER" },
        })}
      />,
    );
    expect(container.textContent).not.toContain("STACK_LINE_MARKER");
    expect(screen.getByText("展开堆栈").getAttribute("aria-expanded")).toBe("false");
  });

  // 测试目的：点击展开渲染 pre 堆栈，再点收起。可能发现的缺陷：堆栈渲染节点缺失或 toggle 不可逆。
  it("点击可展开/收起 stack 且用 pre 标签渲染", () => {
    const { container } = render(
      <LogEntryCard
        entry={makeEntry({ error: { type: "E", message: "m", stack: "line1\nline2" } })}
      />,
    );
    fireEvent.click(screen.getByText("展开堆栈"));
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("line1");
    expect(pre?.textContent).toContain("line2");
    fireEvent.click(screen.getByText("收起堆栈"));
    expect(container.querySelector("pre")).toBeNull();
  });

  // 测试目的：无 stack 时不渲染展开堆栈按钮。可能发现的缺陷：条件判断遗漏导致空堆栈按钮。
  it("error 无 stack 时不渲染堆栈按钮", () => {
    render(<LogEntryCard entry={makeEntry({ error: { type: "E", message: "m" } })} />);
    expect(screen.queryByText("展开堆栈")).toBeNull();
  });

  // 测试目的：error 为 null 时不渲染 error 区。可能发现的缺陷：null 未被短路判断导致读取属性崩溃。
  it("error 为 null 时不渲染 error 区", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ error: null })} />);
    expect(container.textContent).not.toContain("⚠");
  });

  // 测试目的：error 为空对象时不渲染 error 区。可能发现的缺陷：仅判断对象存在渲染出 "⚠ ERROR:" 空壳。
  it("error 为空对象（三字段全缺）时不渲染 error 区", () => {
    const { container } = render(<LogEntryCard entry={makeEntry({ error: {} })} />);
    expect(container.textContent).not.toContain("⚠");
  });

  // 测试目的：仅有 message 无 type 时 type 兜底为 ERROR。可能发现的缺陷：缺少兜底渲染出 "⚠ :" 或 undefined。
  it("仅 message 无 type 时 type 兜底显示 ERROR 且不含 undefined", () => {
    const { container } = render(
      <LogEntryCard entry={makeEntry({ error: { message: "only msg" } })} />,
    );
    expect(container.textContent).toContain("ERROR");
    expect(container.textContent).toContain("only msg");
    expect(container.textContent).not.toContain("undefined");
  });

  // 测试目的：仅有 stack 时 error 区仍渲染且可展开。可能发现的缺陷：hasError 判断漏掉 stack 分支导致堆栈被吞。
  it("仅有 stack 时 error 区仍渲染且堆栈可展开", () => {
    const { container } = render(
      <LogEntryCard entry={makeEntry({ error: { stack: "ONLY_STACK" } })} />,
    );
    expect(container.textContent).toContain("⚠");
    fireEvent.click(screen.getByText("展开堆栈"));
    expect(container.textContent).toContain("ONLY_STACK");
  });
});

describe("LogEntryCard 截断提示", () => {
  // 测试目的：truncated=true 显示提示。可能发现的缺陷：字段未接线，用户无从得知内容不完整。
  it("truncated 为 true 显示 '内容已截断'", () => {
    render(<LogEntryCard entry={makeEntry({ truncated: true })} />);
    expect(screen.getByText("内容已截断")).toBeTruthy();
  });

  // 测试目的：truncated 为 false/缺省不显示提示。可能发现的缺陷：条件写成非空判断导致恒显示。
  it("truncated 为 false 或缺省时不显示提示", () => {
    const { unmount } = render(<LogEntryCard entry={makeEntry({ truncated: false })} />);
    expect(screen.queryByText("内容已截断")).toBeNull();
    unmount();
    render(<LogEntryCard entry={makeEntry()} />);
    expect(screen.queryByText("内容已截断")).toBeNull();
  });
});

describe("LogEntryCard 健壮性", () => {
  // 测试目的：全字段齐备的复杂条目一次性渲染所有区块不崩溃。可能发现的缺陷：多区块同时渲染时 key/结构冲突。
  it("data + error + truncated 同时存在时全部区块渲染", () => {
    const { container } = render(
      <LogEntryCard
        entry={makeEntry({
          level: "CRITICAL",
          data: { a: 1 },
          error: { type: "T", message: "M", stack: "S" },
          truncated: true,
        })}
      />,
    );
    expect(screen.getByText(/data（1 字段）/)).toBeTruthy();
    expect(container.textContent).toContain("T");
    expect(container.textContent).toContain("M");
    expect(screen.getByText("内容已截断")).toBeTruthy();
    expect(screen.getByText("展开元数据")).toBeTruthy();
  });

  // 测试目的：超长 msg/event 不抛错（截断由 CSS 负责）。可能发现的缺陷：渲染层做字符串切片产生越界错误。
  it("超长 event/msg 渲染不抛错", () => {
    const long = "x".repeat(5000);
    expect(() =>
      render(<LogEntryCard entry={makeEntry({ event: long, msg: long })} />),
    ).not.toThrow();
  });

  // 测试目的：data 含 null/嵌套/特殊字符时展开不崩溃。可能发现的缺陷：递归渲染未处理 null 触发 typeof 判定错误。
  it("data 含 null 与嵌套结构时展开不崩溃", () => {
    const { container } = render(
      <LogEntryCard
        entry={makeEntry({
          data: { nil: null, nested: { deep: [1, 2] }, arr: [1, 2, 3] },
        })}
      />,
    );
    expect(() => fireEvent.click(screen.getByText(/data（3 字段）/))).not.toThrow();
    expect(container.textContent).toContain("nil:");
  });
});
