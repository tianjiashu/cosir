import { describe, expect, it } from "vitest";

import { changeProvider, emptyForm, selectClaudeModel, toInput } from "@/lib/model-config-form";

describe("Claude subscription connection form", () => {
  it("clears API credentials and waits for a model selected from the account catalog", () => {
    const form = changeProvider({ ...emptyForm, configName: "我的 Claude", apiKey: "previous-key", baseUrl: "https://other.example" }, "claude_subscription");
    expect(toInput(form)).toMatchObject({ provider: "claude_subscription", config_name: "我的 Claude", base_url: "https://api.anthropic.com", api_key: "", model_name: "" });
  });

  it("does not silently reuse a subscription connection as an API-key connection", () => {
    const form = changeProvider(changeProvider(emptyForm, "claude_subscription"), "openai");
    expect(toInput(form)).toMatchObject({ provider: "openai", api_key: "", base_url: "", model_name: "" });
  });

  it("applies the selected model's context and capabilities without changing the config name", () => {
    const form = selectClaudeModel({ ...emptyForm, provider: "claude_subscription", configName: "个人账号" }, {
      id: "claude-new", display_name: "Claude New", context_window_k: 1000,
      supports_thinking: true, supports_reasoning_effort: false, supports_image: true,
    });
    expect(toInput(form)).toMatchObject({ config_name: "个人账号", model_name: "claude-new", context_window_k: 1000, supports_thinking: true, supports_reasoning_effort: false, supports_image: true });
  });
});
