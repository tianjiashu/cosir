import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { StatusDot } from "@/components/ui/status-dot";

describe("StatusDot", () => {
  it("只是视觉提示，按色调标记状态", () => {
    const html = renderToStaticMarkup(<StatusDot tone="attention" />);
    expect(html).toContain('aria-hidden="true"');
    expect(html).toContain('data-status-dot="attention"');
    expect(html).toContain("bg-destructive");
    expect(renderToStaticMarkup(<StatusDot tone="activity" />)).toContain("bg-info");
  });
});
