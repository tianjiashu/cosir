import { describe, expect, it } from "vitest";

import {
  FILE_ATTACHMENT_TOKEN_PREFIX,
  FILE_ATTACHMENT_TOKEN_SUFFIX,
  renderInlineAttachmentHtml,
} from "@/components/composer/inline-attachment-input";

describe("inline attachment HTML rendering", () => {
  it("renders plain text exactly once when there are no file tokens", () => {
    expect(renderInlineAttachmentHtml("我帅吗", [])).toBe("我帅吗");
  });

  it("escapes plain text and preserves line breaks", () => {
    expect(renderInlineAttachmentHtml("<我帅吗>\n下一行", [])).toBe("&lt;我帅吗&gt;<br>下一行");
  });

  it("renders a known file token as one non-editable chip", () => {
    const value = `请查看 ${FILE_ATTACHMENT_TOKEN_PREFIX}file-1${FILE_ATTACHMENT_TOKEN_SUFFIX}`;
    const html = renderInlineAttachmentHtml(value, [{ id: "file-1", name: "设计说明.md" }]);

    expect(html).toContain("请查看 ");
    expect(html).toContain('data-file-id="file-1"');
    expect(html).toContain("设计说明.md");
    expect(html.match(/data-file-id="file-1"/g)).toHaveLength(1);
  });

  it("renders an unknown token as a safe attachment capsule", () => {
    const value = `${FILE_ATTACHMENT_TOKEN_PREFIX}missing${FILE_ATTACHMENT_TOKEN_SUFFIX}`;

    const html = renderInlineAttachmentHtml(value, []);
    expect(html).toContain("附件已失效");
    expect(html).not.toContain(value);
  });

  it("hides the HTML comment wrapper while editing a canonical message", () => {
    const html = renderInlineAttachmentHtml(
      "请查看 <!-- [[cosir-file:file-1]] -->",
      [{ id: "file-1", name: "notes.md" }],
    );

    expect(html).not.toContain("<!--");
    expect(html).not.toContain("-->");
    expect(html).toContain('data-file-id="file-1"');
  });

  it("renders image tokens as a distinct inline attachment capsule", () => {
    const imageId = "a".repeat(64);
    const html = renderInlineAttachmentHtml(
      `前文[[cosir-image:${imageId}]]后文`,
      [{ id: `cosir-attachment://${imageId}`, name: "截图.png", kind: "image", tokenId: imageId }],
    );

    expect(html).toContain("cosir-inline-image-token");
    expect(html).toContain("图片：截图.png");
    expect(html).toContain(`data-attachment-id="cosir-attachment://${imageId}"`);
  });

  it("renders an unmatched token as synchronizing while the draft is still being written", () => {
    const value = `${FILE_ATTACHMENT_TOKEN_PREFIX}file-1${FILE_ATTACHMENT_TOKEN_SUFFIX}`;

    const html = renderInlineAttachmentHtml(value, [], { unmatchedAsPending: true });

    expect(html).toContain("附件同步中");
    expect(html).not.toContain("附件已失效");
    expect(html).toContain("cosir-inline-file-token-pending");
  });
});
