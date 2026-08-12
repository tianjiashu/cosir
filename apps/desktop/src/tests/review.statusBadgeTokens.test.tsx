// @vitest-environment happy-dom
/**
 * StatusBadge token 显示回归验证。
 *
 * 历史：StatusBadge 曾用 `(totalTokens !== undefined && totalTokens > 0)` 作为整块
 * 渲染条件，导致后端缓存命中等只下发 input/output 不下发 total 时，整块（含
 * input/output）被吞。该缺陷已修复——现改为 input/output/total 任一有值即展示，
 * total 缺失时由前端累加 input+output 兜底（见 review.statusBadgeTokensVacuum）。
 *
 * 本文件聚焦验证：「token 文本是否出现 undefined / NaN」「全缺失时不展示」，
 * 以及「修复后 total 缺失但 input/output 有值时正确累加展示」。
 *
 * @module tests/review.statusBadgeTokens
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

describe("StatusBadge token 显示", () => {
  it("仅 input/output 有值而 total_tokens 缺失时，前端累加展示（不显示 undefined）", () => {
    const { container } = renderBadge({
      duration_ms: 1500,
      input_tokens: 120,
      output_tokens: 30,
      // total_tokens 故意缺失
    });
    const text = container.textContent ?? "";
    expect(text).not.toContain("undefined");
    expect(text).toContain("输入 120 / 输出 30 / 总计 150 tokens");
  });

  it("total_tokens 为 0 也展示（不再被整块吞，避免隐藏无消耗的合法结果）", () => {
    const { container } = renderBadge({
      duration_ms: 100,
      total_tokens: 0,
    });
    const text = container.textContent ?? "";
    expect(text).toContain("总计 0 tokens");
  });

  it("total_tokens 正常时按 输入 X / 输出 Y / 总计 Z 格式展示", () => {
    const { container } = renderBadge({
      duration_ms: 1500,
      input_tokens: 120,
      output_tokens: 30,
      total_tokens: 150,
    });
    const text = container.textContent ?? "";
    expect(text).toContain("输入 120 / 输出 30 / 总计 150 tokens");
  });

  it("total_tokens 缺失且不提供 input/output 时不展示任何 token 文本", () => {
    const { container } = renderBadge({ duration_ms: 800 });
    const text = container.textContent ?? "";
    expect(text).not.toContain("tokens");
  });
});
