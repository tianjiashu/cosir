import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WebPageIcon } from "@/components/links/link-target-icon";
import { brandIconPath } from "@/lib/links/site-icon";

describe("brandIconPath", () => {
  it("matches a registered domain and its subdomains, not lookalikes", () => {
    expect(brandIconPath("github.com")).toBeTruthy();
    expect(brandIconPath("gist.github.com")).toBe(brandIconPath("github.com"));
    expect(brandIconPath("notgithub.com")).toBeNull();
    expect(brandIconPath("github.com.evil.io")).toBeNull();
  });
});

describe("WebPageIcon", () => {
  // 目的：渲染链接不联网；潜在缺陷：模型写的主机名一显示就被请求 favicon。
  it("draws every link icon offline, with no image to fetch", () => {
    for (const url of ["https://github.com/a", "https://unknown.example", "http://localhost:3000", "http://10.0.0.8", "mailto:a@b.c", "about:blank"]) {
      const html = renderToStaticMarkup(<WebPageIcon url={url} />);
      expect(html).toContain("<svg");
      expect(html).not.toContain("<img");
    }
  });
});
