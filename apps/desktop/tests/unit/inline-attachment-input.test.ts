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
});
