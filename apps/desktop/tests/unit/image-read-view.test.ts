import { describe, expect, it } from "vitest";

import { readImageReadDisplay } from "@/components/assistant-ui/tools/image-read-display";
import { imageReadView } from "@/components/assistant-ui/tools/details/image-read-view";
import { routeToolPart } from "@/components/assistant-ui/tools/tool-renderer-registry";

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
    expect(routeToolPart("read_image", { presentation: {}, display_data: DISPLAY })).toBe("details");
    expect(routeToolPart("read_image", { presentation: {}, display_data: { kind: "image-read" } })).toBe("fallback");
  });

  it("summarizes image count and partial failures", () => {
    expect(imageReadView.summary(DISPLAY, "completed")).toBe("1 张，1 张未读取");
    expect(imageReadView.summary({ ...DISPLAY, failures: [] }, "completed")).toBe("1 张");
  });
});
