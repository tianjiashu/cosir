import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { EnvironmentFieldControl } from "@/components/environment-configuration-form";
import type { EnvironmentField } from "@/lib/api/configuration";

const maskedSecret: EnvironmentField = {
  name: "FIRECRAWL_API_KEY",
  type: "string",
  component: "password",
  label: "API Key",
  secret: true,
  default: null,
  value: null,
  disk_value: null,
  configured: true,
  masked: true,
  source: "file",
  process_override: false,
  options: [],
  placeholder: null,
  clearable: true,
  minimum: null,
  maximum: null,
};

function render(pendingClear: boolean) {
  const noop = () => undefined;
  return renderToStaticMarkup(
    <EnvironmentFieldControl
      field={maskedSecret}
      value=""
      onChange={noop}
      onToggle={noop}
      onClear={noop}
      pendingClear={pendingClear}
      onUndoClear={noop}
    />,
  );
}

describe("EnvironmentFieldControl", () => {
  it("已配置的密钥默认提示留空保持不变，并提供清除按钮", () => {
    const markup = render(false);
    expect(markup).toContain("已配置（留空保持不变）");
    expect(markup).toContain("清除 API Key");
    expect(markup).not.toContain("将清除");
  });

  it("点清除后显示「将清除」与撤销，不再提示留空保持不变", () => {
    const markup = render(true);
    expect(markup).toContain("将清除");
    expect(markup).toContain("撤销清除 API Key");
    expect(markup).toContain("保存后将清除已保存值");
    expect(markup).not.toContain("已配置（留空保持不变）");
  });
});
