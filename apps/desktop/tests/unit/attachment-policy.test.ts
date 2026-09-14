import { describe, expect, it } from "vitest";

import {
  contentTypeFor,
  fileName,
  isImagePath,
} from "@/components/composer/attachment-policy";

describe("attachment picker policy", () => {
  it("normalizes filenames and image MIME types across Windows paths", () => {
    expect(fileName("C:\\workspace\\screen.PNG")).toBe("screen.PNG");
    expect(contentTypeFor("C:\\workspace\\screen.PNG")).toBe("image/png");
    expect(isImagePath("C:\\workspace\\screen.PNG")).toBe(true);
    expect(isImagePath("C:\\workspace\\notes.md")).toBe(false);
  });

  it("treats unknown extensions as regular files", () => {
    expect(contentTypeFor("C:\\outside\\notes.md")).toBe("application/octet-stream");
    expect(isImagePath("C:\\outside\\notes.md")).toBe(false);
  });
});
