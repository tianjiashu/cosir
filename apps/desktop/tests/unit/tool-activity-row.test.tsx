import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ToolActivityRow } from "@/components/assistant-ui/tools/tool-activity-row";

describe("ToolActivityRow", () => {
  it("exposes an accessible open affordance and keeps unavailable rows disabled", () => {
    const html = renderToStaticMarkup(
      <ToolActivityRow
        icon={<span aria-hidden="true">agent</span>}
        title="审查代码"
        meta="Reviewer"
        status="running"
        onOpen={null}
        openLabel="打开子 Agent：审查代码"
        disabled
      />,
    );

    expect(html).toContain('data-testid="tool-activity-row"');
    expect(html).toContain('aria-label="审查代码"');
    expect(html).toContain("disabled");
    expect(html).toContain("审查代码");
    expect(html).toContain("Reviewer");
    expect(html).toContain("执行中");
  });
});
