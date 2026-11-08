import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentConfigurationDraftTool } from "@/components/conversation/tools/configuration-draft/agent-configuration-draft-tool";
import { WorkbenchProvider } from "@/lib/workbench/workbench-context";

describe("AgentConfigurationDraftTool", () => {
  it.each([true, false])("可编辑配置草稿时只显示编辑入口，不显示放弃按钮 (interactive=%s)", (interactive) => {
    const markup = renderToStaticMarkup(
      <WorkbenchProvider workspaceId={1}>
        <AgentConfigurationDraftTool
          interactive={interactive}
          artifact={{presentation: "activity",
            backendStatus: "completed",
            awaitingUser: false,
            background: false,
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

    if (interactive) expect(markup).toContain("编辑配置");
    else expect(markup).not.toContain("编辑配置");
    expect(markup).not.toContain("放弃");
  });
});
