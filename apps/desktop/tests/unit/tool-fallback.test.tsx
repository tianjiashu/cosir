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
      display_data: { status_hint: "路径无效" },
      // 后端把受控短提示（status_hint）写进 part.error，二者一致。
      error: "路径无效",
      errorCode: "path_outside_workspace",
    });

    expect(html).toContain("读取文件");
    expect(html).toContain("text-destructive");
    expect(html).toContain("路径无效");
    expect(html).not.toContain("未知工具");
  });

  it("成功态不显示实现说明类副文案", () => {
    const html = renderFallback({
      backendStatus: "completed",
      presentation: { verb: "等待结果", icon: "clock" },
      display_data: null,
      error: null,
      errorCode: null,
    });

    expect(html).toContain("等待结果");
    expect(html).not.toContain("仅展示后端状态");
  });
});
