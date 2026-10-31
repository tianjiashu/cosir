import { describe, expect, it, vi } from "vitest";

// 桌面宿主给出本机路径；名字以 clipboard 开头的文件模拟剪贴板里的内存数据（没有路径）。
vi.mock("@/lib/desktop-host", () => ({
  desktopHost: {
    files: { pathOf: (file: File) => (file.name.startsWith("clipboard") ? "" : `/workspace/${file.name}`) },
  },
}));

import { addComposerFiles } from "@/components/composer/assistant-composer-attachments";
import { getLocalAttachmentById } from "@/lib/assistant/attachments/local-attachment-registry";

const textFile = (name: string) => new File([], name, { type: "text/plain" });

describe("统一 Composer 附件接入", () => {
  it("普通文件登记本机路径并只返回文档 token，图片只进入图片附件集合", async () => {
    const image = new File(["image"], "photo.png", { type: "image/png" });
    const addImage = vi.fn();
    const result = await addComposerFiles([textFile("notes.md"), image], { addImage, isCurrent: () => true });

    expect(addImage).toHaveBeenCalledExactlyOnceWith(image);
    expect(result.problems).toEqual([]);
    expect(result.attachments).toEqual([{ id: expect.any(String), name: "notes.md" }]);
    expect(getLocalAttachmentById(result.attachments[0].id)).toMatchObject({ path: "/workspace/notes.md", contentType: "text/plain" });
  });

  it("在入口拒绝没有本机路径的普通文件和不支持的图片", async () => {
    const addImage = vi.fn();
    const result = await addComposerFiles([
      textFile("clipboard.md"),
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
      new File([], "failed.png", { type: "image/png" }), textFile("accepted.md"),
    ], { addImage: async () => { throw new Error("adapter failed"); }, isCurrent: () => true });
    expect(result.attachments.map(({ name }) => name)).toEqual(["accepted.md"]);
    expect(result.problems).toEqual([{ name: "failed.png", reason: "add-failed" }]);
  });

  it("会话在异步添加期间失效后，停止处理并丢弃待插入 token", async () => {
    let current = true;
    const addImage = vi.fn(async () => { current = false; });
    const result = await addComposerFiles([
      textFile("first.md"), new File([], "photo.png", { type: "image/png" }), textFile("last.md"),
    ], { addImage, isCurrent: () => current });
    expect(result).toEqual({ attachments: [], problems: [] });
    expect(addImage).toHaveBeenCalledTimes(1);
  });
});
