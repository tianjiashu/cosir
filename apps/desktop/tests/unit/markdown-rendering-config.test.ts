import { describe, expect, it } from "vitest";

import { code } from "@streamdown/code";
import { cjk } from "@streamdown/cjk";
import { math } from "@streamdown/math";
import { mermaid } from "@streamdown/mermaid";

import { markdownPlugins } from "@/components/markdown-rendering-config";

describe("markdown rendering plugins", () => {
  it("keeps the four supported Streamdown plugins in one stable configuration", () => {
    expect(markdownPlugins).toEqual({ code, math, mermaid, cjk });
  });
});
