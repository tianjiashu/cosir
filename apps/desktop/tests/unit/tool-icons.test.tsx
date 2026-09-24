import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ToolIcon } from "@/components/assistant-ui/tools/tool-icons";

describe("ToolIcon", () => {
  it("renders the declared Diff icon", () => {
    const html = renderToStaticMarkup(<ToolIcon name="git-compare" aria-hidden="true" />);

    expect(html).toContain("lucide-git-compare");
  });

  it.each([
    ["info", "lucide-info"],
    ["send", "lucide-send"],
    ["clock", "lucide-clock"],
  ])("renders the declared child-agent %s icon", (name, className) => {
    const html = renderToStaticMarkup(<ToolIcon name={name} aria-hidden="true" />);

    expect(html).toContain(className);
    expect(html).not.toContain("lucide-wrench");
  });
});
