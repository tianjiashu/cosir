import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ToolFallback } from "@/components/assistant-ui/tools/tool-fallback";

function renderFallback(artifact: Record<string, unknown>) {
  return renderToStaticMarkup(
    <ToolFallback
      type="tool-call"
      toolName="read_file"
      toolCallId="call-1"
      args={{}}
      argsText="{}"
      status={{ type: "incomplete", reason: "error" } as never}
      addResult={() => undefined}
      resume={() => undefined}
      respondToApproval={() => undefined}
      artifact={artifact}
    />,
  );
}

describe("ToolFallback", () => {
  it("uses the declared tool presentation instead of calling read_file unknown", () => {
    const html = renderFallback({
      backendStatus: "failed",
      presentation: { verb: "读取文件", icon: "eye", expand_layout: "none" },
      data: null,
      error: "路径不在工作区内",
      errorCode: "path_outside_workspace",
    });

    expect(html).toContain("工具执行失败：读取文件");
    expect(html).toContain("路径不在工作区内");
    expect(html).not.toContain("未知工具");
  });
});
