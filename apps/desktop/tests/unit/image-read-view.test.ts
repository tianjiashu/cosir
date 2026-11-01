import { describe, expect, it } from "vitest";

import { readImageReadDisplay } from "@/components/assistant-ui/tools/image-read-display";
import { resolveCallView } from "@/components/assistant-ui/tools/call-view-registry";
import { imageReadView } from "@/components/assistant-ui/tools/details/image-read-view";
import type { CallViewContext } from "@/components/assistant-ui/tools/call-view";
import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

const SHA = "b".repeat(64);
const DISPLAY = {
  kind: "image-read",
  images: [{ path: "docs/a.png", asset_id: SHA, width: 16, height: 10 }],
  failures: [{ path: "notes.txt", hint: "不是有效图片" }],
};

describe("image-read display", () => {
  it("reads images and failures", () => {
    expect(readImageReadDisplay(DISPLAY)).toEqual({
      images: [{ path: "docs/a.png", assetId: SHA, width: 16, height: 10 }],
      failures: [{ path: "notes.txt", hint: "不是有效图片" }],
    });
  });

  // 目的：图片地址只来自合法的附件 id；潜在缺陷：展示数据里的任意字符串被拼进图片地址。
  it("drops entries with malformed asset ids and rejects malformed payloads", () => {
    const display = readImageReadDisplay({ ...DISPLAY, images: [{ path: "x.png", asset_id: "../../etc" }] });
    expect(display?.images).toEqual([]);
    expect(readImageReadDisplay({ kind: "image-read", images: [] })).toBeNull();
  });

  it("routes read_image by display kind, falling back on bad payloads", () => {
    const artifact = (display_data: Record<string, unknown>): ToolArtifact => ({
      backendStatus: "completed", presentation: {}, heading: null, awaitingUser: false, background: false, display_data, error: null, errorCode: null,
    });
    expect(resolveCallView(artifact(DISPLAY))?.view).toBe(imageReadView);
    expect(resolveCallView(artifact({ kind: "image-read" }))).toBeNull();
  });

  it("summarizes image count and partial failures", () => {
    const context = {} as CallViewContext;
    expect(imageReadView.meta!(DISPLAY, context)).toBe("1 张，1 张未读取");
    expect(imageReadView.meta!({ ...DISPLAY, failures: [] }, context)).toBe("1 张");
  });
});
