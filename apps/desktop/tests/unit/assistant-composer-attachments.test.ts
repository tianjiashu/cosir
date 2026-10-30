import { describe, expect, it, vi } from "vitest";

import { addComposerFiles } from "@/components/composer/assistant-composer-attachments";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";

function nativeFile(name: string) {
  return registerLocalAttachment(new File([], name, { type: "text/plain" }), {
    id: name, name, path: `/workspace/${name}`, contentType: "text/plain", kind: "file",
  });
}

describe("统一 Composer 附件接入", () => {
  it("普通文件只返回文档 token，图片只进入图片附件集合", async () => {
    const image = new File(["image"], "photo.png", { type: "image/png" });
    const addImage = vi.fn();
    const result = await addComposerFiles([nativeFile("notes.md"), image], { addImage, isCurrent: () => true });

    expect(addImage).toHaveBeenCalledExactlyOnceWith(image);
    expect(result).toEqual({ attachments: [{ id: "notes.md", name: "notes.md" }], problems: [] });
  });

  it("在入口拒绝没有真实路径的普通文件和不支持的图片", async () => {
    const addImage = vi.fn();
    const result = await addComposerFiles([
      new File([], "clipboard.md", { type: "text/plain" }),
      new File([], "animation.gif", { type: "image/gif" }),
    ], { addImage, isCurrent: () => true });
    expect(addImage).not.toHaveBeenCalled();
    expect(result).toEqual({ attachments: [], problems: [
      { name: "clipboard.md", reason: "local-path-required" },
      { name: "animation.gif", reason: "unsupported-image" },
    ] });
  });

  it("图片添加失败不阻断其他文件，并返回明确问题", async () => {
    const result = await addComposerFiles([
      new File([], "failed.png", { type: "image/png" }), nativeFile("accepted.md"),
    ], { addImage: async () => { throw new Error("adapter failed"); }, isCurrent: () => true });
    expect(result.attachments.map(({ id }) => id)).toEqual(["accepted.md"]);
    expect(result.problems).toEqual([{ name: "failed.png", reason: "add-failed" }]);
  });

  it("会话在异步添加期间失效后，停止处理并丢弃待插入 token", async () => {
    let current = true;
    const addImage = vi.fn(async () => { current = false; });
    const result = await addComposerFiles([
      nativeFile("first.md"), new File([], "photo.png", { type: "image/png" }), nativeFile("last.md"),
    ], { addImage, isCurrent: () => current });
    expect(result).toEqual({ attachments: [], problems: [] });
    expect(addImage).toHaveBeenCalledTimes(1);
  });
});
