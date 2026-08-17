// @vitest-environment happy-dom
/**
 * LogStatsBar 单元测试。
 *
 * 覆盖：后端级别计数呈现、缺失级别回退 0、「全部」入口、选中态高亮与
 * aria-pressed、点击回调参数、props 变化后计数更新等。
 *
 * @module tests/logs.logStatsBar
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { LogStatsBar } from "@/components/logs/LogStatsBar";

afterEach(() => {
  cleanup();
});

describe("LogStatsBar 计数", () => {
  // 测试目的：各级别 badge 显示后端计数，缺失级别回退 0。可能发现的缺陷：读取 key 拼写错误导致计数错位。
  it("各级别 badge 显示后端计数，缺失级别回退 0", () => {
    render(
      <LogStatsBar
        levelCounts={{ ERROR: 3, WARNING: 1, INFO: 2, DEBUG: 1 }}
        total={7}
        activeLevel=""
        onSelectLevel={() => {}}
      />,
    );
    expect(screen.getByText("ERROR 3")).toBeTruthy();
    expect(screen.getByText("WARNING 1")).toBeTruthy();
    expect(screen.getByText("INFO 2")).toBeTruthy();
    expect(screen.getByText("DEBUG 1")).toBeTruthy();
  });

  // 测试目的：空 levelCounts 时四个级别均显示 0 且栏位常驻。可能发现的缺陷：空数据提前 return 导致筛选入口消失。
  it("空 levelCounts 时四个级别均显示 0", () => {
    render(<LogStatsBar levelCounts={{}} total={0} activeLevel="" onSelectLevel={() => {}} />);
    expect(screen.getByText("ERROR 0")).toBeTruthy();
    expect(screen.getByText("WARNING 0")).toBeTruthy();
    expect(screen.getByText("INFO 0")).toBeTruthy();
    expect(screen.getByText("DEBUG 0")).toBeTruthy();
  });

  // 测试目的：「全部」入口显示后端 total。可能发现的缺陷：total 未透传导致「全部」恒为 0。
  it("「全部」入口显示 total", () => {
    render(<LogStatsBar levelCounts={{}} total={42} activeLevel="" onSelectLevel={() => {}} />);
    expect(screen.getByText("全部 42")).toBeTruthy();
  });

  // 测试目的：CRITICAL 等未纳入统计的级别不影响四个计数。可能发现的缺陷：未知级别写入造成 NaN 或 undefined。
  it("CRITICAL/未知级别不计入四个统计项且不产生 NaN", () => {
    const { container } = render(
      <LogStatsBar
        levelCounts={{ CRITICAL: 99, INFO: 1 }}
        total={100}
        activeLevel=""
        onSelectLevel={() => {}}
      />,
    );
    expect(screen.getByText("INFO 1")).toBeTruthy();
    expect(container.textContent).not.toContain("NaN");
    expect(container.textContent).not.toContain("undefined");
  });

  // 测试目的：只渲染「全部」与四个已知级别共 5 个按钮。可能发现的缺陷：未知级别被误渲染。
  it("仅渲染「全部」与 ERROR/WARNING/INFO/DEBUG 共五个按钮", () => {
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="" onSelectLevel={() => {}} />,
    );
    expect(container.querySelectorAll("button").length).toBe(5);
    expect(container.textContent).not.toContain("CRITICAL");
  });

  // 测试目的：levelCounts/total 变化后计数随之更新。可能发现的缺陷：依赖缺失导致计数陈旧。
  it("levelCounts/total 变化后计数随之更新", () => {
    const { rerender } = render(
      <LogStatsBar levelCounts={{ ERROR: 1 }} total={1} activeLevel="" onSelectLevel={() => {}} />,
    );
    expect(screen.getByText("ERROR 1")).toBeTruthy();
    rerender(
      <LogStatsBar
        levelCounts={{ ERROR: 2, DEBUG: 1 }}
        total={3}
        activeLevel=""
        onSelectLevel={() => {}}
      />,
    );
    expect(screen.getByText("ERROR 2")).toBeTruthy();
    expect(screen.getByText("DEBUG 1")).toBeTruthy();
    expect(screen.getByText("全部 3")).toBeTruthy();
  });
});

describe("LogStatsBar 选中态", () => {
  // 测试目的：activeLevel 命中级别时该按钮 aria-pressed=true，其余为 false。可能发现的缺陷：比较逻辑写反导致全选中。
  it("activeLevel 命中级别时该按钮 aria-pressed=true，其余为 false", () => {
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="WARNING" onSelectLevel={() => {}} />,
    );
    const buttons = Array.from(container.querySelectorAll("button"));
    const pressed = buttons.filter((b) => b.getAttribute("aria-pressed") === "true");
    expect(pressed.length).toBe(1);
    expect(pressed[0].textContent).toContain("WARNING");
  });

  // 测试目的：选中级别 badge 带 ring 高亮类。可能发现的缺陷：高亮类未应用，用户无法感知当前筛选。
  it("选中级别 badge 应用 ring-2 高亮类", () => {
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="ERROR" onSelectLevel={() => {}} />,
    );
    const ringEls = container.querySelectorAll(".ring-2");
    expect(ringEls.length).toBe(1);
    expect(ringEls[0].textContent).toContain("ERROR");
  });

  // 测试目的：activeLevel 为空串时「全部」按钮选中且仅它高亮。可能发现的缺陷：空串误匹配到级别按钮。
  it("activeLevel 为空串时「全部」按钮选中且仅它高亮", () => {
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="" onSelectLevel={() => {}} />,
    );
    const ringEls = container.querySelectorAll(".ring-2");
    expect(ringEls.length).toBe(1);
    expect(ringEls[0].textContent).toContain("全部");
    const buttons = Array.from(container.querySelectorAll("button"));
    expect(buttons[0].getAttribute("aria-pressed")).toBe("true");
    expect(buttons.slice(1).every((b) => b.getAttribute("aria-pressed") === "false")).toBe(true);
  });

  // 测试目的：activeLevel 为不在栏内的级别时不误高亮。可能发现的缺陷：模糊匹配导致错误高亮。
  it("activeLevel 为 CRITICAL（不在栏内）时无高亮", () => {
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="CRITICAL" onSelectLevel={() => {}} />,
    );
    expect(container.querySelectorAll(".ring-2").length).toBe(0);
  });

  // 测试目的：各级别 badge 变体配色正确。可能发现的缺陷：BADGE_VARIANT 映射错配。
  it("各级别应用对应 Badge 变体类（destructive/amber/secondary/outline）", () => {
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="" onSelectLevel={() => {}} />,
    );
    expect(container.querySelector(".bg-destructive")).not.toBeNull();
    expect(container.querySelector(".bg-amber-100")).not.toBeNull();
    expect(container.querySelector(".bg-secondary")).not.toBeNull();
    expect(container.querySelector(".text-foreground")).not.toBeNull();
  });
});

describe("LogStatsBar 点击回调", () => {
  // 测试目的：点击 ERROR badge 以精确参数回调一次。可能发现的缺陷：回调传错级别或触发多次。
  it("点击 ERROR 以 'ERROR' 调用 onSelectLevel 一次", () => {
    const onSelect = vi.fn();
    render(
      <LogStatsBar levelCounts={{ ERROR: 1 }} total={1} activeLevel="" onSelectLevel={onSelect} />,
    );
    fireEvent.click(screen.getByText("ERROR 1"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("ERROR");
  });

  // 测试目的：四个级别点击分别回传各自级别。可能发现的缺陷：闭包捕获错误导致所有按钮回调同一级别。
  it("四个级别点击分别回传各自级别（闭包无捕获错误）", () => {
    const onSelect = vi.fn();
    render(<LogStatsBar levelCounts={{}} total={0} activeLevel="" onSelectLevel={onSelect} />);
    fireEvent.click(screen.getByText("ERROR 0"));
    fireEvent.click(screen.getByText("WARNING 0"));
    fireEvent.click(screen.getByText("INFO 0"));
    fireEvent.click(screen.getByText("DEBUG 0"));
    expect(onSelect.mock.calls.map((c) => c[0])).toEqual([
      "ERROR",
      "WARNING",
      "INFO",
      "DEBUG",
    ]);
  });

  // 测试目的：点击「全部」以空串回调。可能发现的缺陷：「全部」未绑定回调导致无法清除筛选。
  it("点击「全部」以空串调用 onSelectLevel", () => {
    const onSelect = vi.fn();
    render(
      <LogStatsBar levelCounts={{}} total={9} activeLevel="INFO" onSelectLevel={onSelect} />,
    );
    fireEvent.click(screen.getByText("全部 9"));
    expect(onSelect).toHaveBeenCalledWith("");
  });

  // 测试目的：计数为 0 的级别按钮仍可点击。可能发现的缺陷：零计数被禁用，用户无法筛选空级别。
  it("计数为 0 的级别按钮未被禁用且可点击", () => {
    const onSelect = vi.fn();
    const { container } = render(
      <LogStatsBar levelCounts={{}} total={0} activeLevel="" onSelectLevel={onSelect} />,
    );
    const buttons = Array.from(container.querySelectorAll("button"));
    expect(buttons.every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
    fireEvent.click(screen.getByText("DEBUG 0"));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  // 测试目的：未点击时不应有任何回调（无副作用渲染）。可能发现的缺陷：渲染期直接调用回调造成死循环刷新。
  it("仅渲染不触发 onSelectLevel（渲染期无副作用）", () => {
    const onSelect = vi.fn();
    render(
      <LogStatsBar levelCounts={{ INFO: 1 }} total={1} activeLevel="" onSelectLevel={onSelect} />,
    );
    expect(onSelect).not.toHaveBeenCalled();
  });
});
