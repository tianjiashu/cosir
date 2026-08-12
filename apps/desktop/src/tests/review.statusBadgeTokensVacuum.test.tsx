// @vitest-environment happy-dom
/**
 * 回归测试：StatusBadge token 不再整块吞噬。
 *
 * 修复前（StatusBadge.tsx:90）：token 行渲染条件为
 *   (totalTokens !== undefined && totalTokens > 0)
 * 后端缓存命中等只下发 input/output 不下发 total 时，整块（含 input/output）消失。
 *
 * 修复后：input/output/total 任一有值即展示；total 缺失时由前端累加 input+output 兜底。
 * 本文件固化该修复，防止回退为「整块吞噬」。
 *
 * @module tests/review.statusBadgeTokensVacuum
 */

import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

vi.mock("@/services/backend", () => ({
  openFileInEditor: vi.fn().mockResolvedValue(undefined),
}));

import { StatusBadge } from "@/components/chat/StatusBadge";

function renderBadge(payload: Record<string, unknown>) {
  return render(<StatusBadge eventType="run_finished" payload={payload} />);
}

describe("StatusBadge token 不再整块吞噬（回归）", () => {
  it("仅 input/output 有值、total_tokens 缺失时，仍展示 token 消耗（前端累加兜底）", () => {
    const { container } = renderBadge({
      duration_ms: 1500,
      input_tokens: 120,
      output_tokens: 30,
    });
    const text = container.textContent ?? "";
    expect(text).toContain("输入 120");
    expect(text).toContain("输出 30");
    expect(text).toContain("总计 150"); // 前端累加 input+output
  });

  it("total_tokens=0 但有 input/output 时，仍展示（不再被整块吞）", () => {
    const { container } = renderBadge({
      duration_ms: 100,
      input_tokens: 5,
      output_tokens: 0,
      total_tokens: 0,
    });
    const text = container.textContent ?? "";
    expect(text).toContain("输入 5");
    expect(text).toContain("输出 0");
  });

  it("后端下发 total 时优先使用后端 total（不重复累加）", () => {
    const { container } = render(
      <StatusBadge
        eventType="run_finished"
        payload={{ duration_ms: 100, input_tokens: 10, output_tokens: 20, total_tokens: 40 }}
      />,
    );
    expect(container.textContent ?? "").toContain("总计 40");
  });
});
