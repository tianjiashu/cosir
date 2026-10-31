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
const pngFile = (name: string) => new File([], name, { type: "image/png" });
/** 草稿里已有 `existing` 张图片的图片集合。 */
const sink = (add: (file: File) => void | Promise<void>, existing = 0) => ({ add, count: () => existing });

describe("统一 Composer 附件接入", () => {
  it("普通文件登记本机路径并只返回文档 token，图片只进入图片附件集合", async () => {
    const image = new File(["image"], "photo.png", { type: "image/png" });
    const addImage = vi.fn();
    const result = await addComposerFiles([textFile("notes.md"), image], { images: sink(addImage), isCurrent: () => true });

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
    ], { images: sink(addImage), isCurrent: () => true });
    expect(addImage).not.toHaveBeenCalled();
    expect(result).toEqual({ attachments: [], problems: [
      { name: "clipboard.md", reason: "local-path-required" },
      { name: "animation.gif", reason: "unsupported-image" },
    ] });
  });

  it("图片添加失败不阻断其他文件，并返回明确问题", async () => {
    const result = await addComposerFiles([
      new File([], "failed.png", { type: "image/png" }), textFile("accepted.md"),
    ], { images: sink(async () => { throw new Error("adapter failed"); }), isCurrent: () => true });
    expect(result.attachments.map(({ name }) => name)).toEqual(["accepted.md"]);
    expect(result.problems).toEqual([{ name: "failed.png", reason: "add-failed" }]);
  });

  it("会话在异步添加期间失效后，停止处理并丢弃待插入 token", async () => {
    let current = true;
    const addImage = vi.fn(async () => { current = false; });
    const result = await addComposerFiles([
      textFile("first.md"), new File([], "photo.png", { type: "image/png" }), textFile("last.md"),
    ], { images: sink(addImage), isCurrent: () => current });
    expect(result).toEqual({ attachments: [], problems: [] });
    expect(addImage).toHaveBeenCalledTimes(1);
  });

  // 目的：超量图片在添加入口就被拦下；潜在缺陷：选了超量图片，发送时逐张上传完才被后端整条拒绝。
  it("超出单条消息图片上限的图片按顺序跳过，普通文件不受影响", async () => {
    const addImage = vi.fn();
    const result = await addComposerFiles([
      pngFile("a.png"), pngFile("b.png"), textFile("notes.md"), pngFile("c.png"),
    ], { images: sink(addImage, 18), isCurrent: () => true });
    expect(addImage.mock.calls.map(([file]) => file.name)).toEqual(["a.png", "b.png"]);
    expect(result.attachments.map(({ name }) => name)).toEqual(["notes.md"]);
    expect(result.problems).toEqual([{ name: "c.png", reason: "image-limit" }]);
  });
});
