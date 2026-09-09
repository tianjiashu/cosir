import { describe, expect, it } from "vitest";

import { resolveWebExtractSiteStatus } from "@/components/assistant-ui/tools/types";

describe("resolveWebExtractSiteStatus", () => {
  it("shows an in-flight site as running while the tool is running", () => {
    expect(resolveWebExtractSiteStatus("pending", "running")).toBe("running");
  });

  it("shows unfinished sites as cancelled when the tool is cancelled", () => {
    expect(resolveWebExtractSiteStatus("pending", "cancelled")).toBe("cancelled");
    expect(resolveWebExtractSiteStatus("running", "cancelled")).toBe("cancelled");
  });

  it("preserves a completed site status during a terminal tool state", () => {
    expect(resolveWebExtractSiteStatus("success", "cancelled")).toBe("success");
    expect(resolveWebExtractSiteStatus("truncated", "failed")).toBe("truncated");
  });
});
