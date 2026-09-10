import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ToolIcon } from "@/components/assistant-ui/tools/tool-icons";

describe("ToolIcon", () => {
  it("renders the declared Diff icon", () => {
    const html = renderToStaticMarkup(<ToolIcon name="git-compare" aria-hidden="true" />);

    expect(html).toContain("lucide-git-compare");
  });
});
