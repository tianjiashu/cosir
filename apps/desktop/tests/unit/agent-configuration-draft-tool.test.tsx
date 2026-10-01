import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentConfigurationDraftTool } from "@/components/assistant-ui/tools/agent-configuration-draft-tool";
import { WorkbenchProvider } from "@/lib/workbench/context";

describe("AgentConfigurationDraftTool", () => {
  it("可编辑配置草稿时只显示编辑入口，不显示放弃按钮", () => {
    const markup = renderToStaticMarkup(
      <WorkbenchProvider workspaceId={1}>
        <AgentConfigurationDraftTool
          type="tool-call"
          toolName="propose-agent-configuration"
          args={{}}
          argsText="{}"
          status={{ type: "complete", reason: "stop" } as never}
          addResult={() => undefined}
          resume={() => undefined}
          respondToApproval={() => undefined}
          artifact={{
            backendStatus: "completed",
            presentation: {},
            display_data: {
              kind: "agent-configuration-draft",
              status: "draft",
              agent_id: "backend-runtime-investigator",
              role: "后端排查",
              description: "收集运行时证据",
              system_prompt: "请完成后端排查。",
            },
            error: null,
            errorCode: null,
          }}
          taskId={3}
          toolCallId="tool-call-1"
        />
      </WorkbenchProvider>,
    );

    expect(markup).toContain("编辑配置");
    expect(markup).not.toContain("放弃");
  });
});
