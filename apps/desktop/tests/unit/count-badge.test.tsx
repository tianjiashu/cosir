import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CountBadge } from "@/components/ui/count-badge";

describe("页头角标", () => {
  it("默认色调：对读屏隐藏，显示传入的数字", () => {
    const html = renderToStaticMarkup(<CountBadge>{3}</CountBadge>);
    expect(html).toContain('aria-hidden="true"');
    expect(html).toContain("bg-foreground text-background");
    expect(html).toMatch(/>3<\/span>$/);
  });

  it("attention 色调与透传的 data 属性（终端角标的「!」）", () => {
    const html = renderToStaticMarkup(<CountBadge data-terminal-badge="attention" tone="attention">!</CountBadge>);
    expect(html).toContain('data-terminal-badge="attention"');
    expect(html).toContain("bg-destructive text-destructive-foreground");
    expect(html).not.toContain("bg-foreground");
    expect(html).toMatch(/>!<\/span>$/);
  });
});
