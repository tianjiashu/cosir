import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RenderErrorCard } from "@/components/render-isolation/render-error-card";

describe("局部渲染降级 UI", () => {
  it("把内容作为纯文本展示，并提供局部重试操作", () => {
    const html = renderToStaticMarkup(
      <RenderErrorCard
        scope="markdown"
        label="Assistant 内容"
        fallbackText="<script>不能作为 HTML 执行</script>"
        onRetry={() => undefined}
      />,
    );

    expect(html).toContain("Assistant 内容暂时无法显示");
    expect(html).toContain("重试显示");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });
});
