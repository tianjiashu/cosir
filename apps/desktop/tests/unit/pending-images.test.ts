import { afterEach, describe, expect, it, vi } from "vitest";

import { uploadAttachment } from "@/lib/assistant/attachments/attachment-upload";
import { holdPendingImage, releasePendingImages, uploadPendingImages } from "@/lib/assistant/attachments/pending-images";
import { newTraceId } from "@/lib/logging/trace-id";

vi.mock("@/lib/assistant/attachments/attachment-upload", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/assistant/attachments/attachment-upload")>()),
  uploadAttachment: vi.fn(),
}));
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));

const uploaded = (id: string) => ({ id, name: "a.png", contentType: "image/png", byteSize: 1, width: 1, height: 1, locator: `cosir-attachment://${id}`, status: "ready" as const });

afterEach(() => vi.mocked(uploadAttachment).mockReset());

describe("待上传图片", () => {
  it("提交前把待上传图片换成附件 locator，其余 part 原样；相同内容只上传一次", async () => {
    const asset = "c".repeat(64);
    vi.mocked(uploadAttachment).mockResolvedValue(uploaded(asset));
    const one = holdPendingImage(new File(["same"], "a.png", { type: "image/png" }));
    const two = holdPendingImage(new File(["same"], "b.png", { type: "image/png" }));
    const parts = [{ type: "text" as const, text: "看图" }, { type: "image" as const, image: one }, { type: "image" as const, image: two }, { type: "image" as const, image: `cosir-attachment://${"d".repeat(64)}` }];

    await expect(uploadPendingImages(7, parts, newTraceId())).resolves.toEqual([
      { type: "text", text: "看图" },
      { type: "image", image: `cosir-attachment://${asset}` },
      { type: "image", image: `cosir-attachment://${asset}` },
      { type: "image", image: `cosir-attachment://${"d".repeat(64)}` },
    ]);
    expect(uploadAttachment).toHaveBeenCalledTimes(1);
    expect(parts[1]).toEqual({ type: "image", image: one });
  });

  it("上传失败抛出可展示的原因，图片仍登记着，重发时能再上传；释放后不再视为待上传", async () => {
    const image = holdPendingImage(new File(["retry"], "retry.png", { type: "image/png" }));
    vi.mocked(uploadAttachment).mockRejectedValueOnce(new Error("timeout"));
    await expect(uploadPendingImages(7, [{ type: "image", image }], newTraceId())).rejects.toThrow("图片「retry.png」上传失败，消息未发出");

    vi.mocked(uploadAttachment).mockResolvedValueOnce(uploaded("e".repeat(64)));
    await expect(uploadPendingImages(7, [{ type: "image", image }], newTraceId())).resolves.toEqual([{ type: "image", image: `cosir-attachment://${"e".repeat(64)}` }]);

    releasePendingImages([{ type: "image", image }]);
    await expect(uploadPendingImages(7, [{ type: "image", image }], newTraceId())).resolves.toEqual([{ type: "image", image }]);
    expect(uploadAttachment).toHaveBeenCalledTimes(2);
  });
});
