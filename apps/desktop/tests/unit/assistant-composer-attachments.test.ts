import { describe, expect, it, vi } from "vitest";

import { addFilesToAssistantComposer } from "@/components/composer/assistant-composer-attachments";

type TestAttachment = {
  id: string;
  name: string;
  type: string;
  file?: File;
};

function createFile(name: string, type = "text/plain") {
  return new File([name], name, { type });
}

describe("Assistant UI Composer 附件接入", () => {
  it("只为运行时确认存在的普通文件返回 token，图片仍由附件条带展示", async () => {
    const file = createFile("notes.md");
    const image = createFile("photo.png", "image/png");
    const attachments: TestAttachment[] = [];
    const addAttachment = vi.fn(async (addedFile: File) => {
      attachments.push({
        id: `runtime-${addedFile.name}`,
        name: addedFile.name,
        type: addedFile.type.startsWith("image/") ? "image" : "file",
        file: addedFile,
      });
    });

    const result = await addFilesToAssistantComposer(
      [{ file, name: file.name }, { file: image, name: image.name }],
      { getAttachments: () => attachments, addAttachment },
      vi.fn(),
    );

    expect(addAttachment).toHaveBeenCalledTimes(2);
    expect(result.attachments).toEqual([{
      id: "runtime-notes.md",
      name: "notes.md",
      kind: "file",
      tokenId: "runtime-notes.md",
    }]);
    expect(result.problems).toEqual([]);
  });

  it("单个附件失败时继续处理后续文件并返回结构化问题", async () => {
    const failedFile = createFile("failed.md");
    const acceptedFile = createFile("accepted.md");
    const attachments: TestAttachment[] = [];
    const onFailure = vi.fn();

    const result = await addFilesToAssistantComposer(
      [
        { file: failedFile, name: failedFile.name },
        { file: acceptedFile, name: acceptedFile.name },
      ],
      {
        getAttachments: () => attachments,
        addAttachment: async (file) => {
          if (file === failedFile) throw new Error("adapter failed");
          attachments.push({ id: "accepted-id", name: file.name, type: "file", file });
        },
      },
      onFailure,
    );

    expect(result.attachments.map(({ id }) => id)).toEqual(["accepted-id"]);
    expect(result.problems).toEqual([{ name: "failed.md", reason: "add-failed" }]);
    expect(onFailure).toHaveBeenCalledWith({
      file: { file: failedFile, name: "failed.md" },
      stage: "add",
      error: expect.any(Error),
    });
  });

  it("已存在的原生选择附件不重复加入，且身份不明确时报告失败", async () => {
    const existingFile = createFile("existing.md");
    const unresolvedFile = createFile("unresolved.md");
    const attachments: TestAttachment[] = [{
      id: "picker-id",
      name: "existing.md",
      type: "file",
      file: existingFile,
    }];
    const addAttachment = vi.fn(async () => undefined);
    const onFailure = vi.fn();

    const result = await addFilesToAssistantComposer(
      [
        { file: existingFile, name: existingFile.name, skipIfAttachmentIdExists: "picker-id" },
        { file: unresolvedFile, name: unresolvedFile.name },
      ],
      { getAttachments: () => attachments, addAttachment },
      onFailure,
    );

    expect(addAttachment).toHaveBeenCalledTimes(1);
    expect(result.attachments).toEqual([]);
    expect(result.problems).toEqual([{ name: "unresolved.md", reason: "add-failed" }]);
    expect(onFailure).toHaveBeenCalledWith({
      file: { file: unresolvedFile, name: "unresolved.md" },
      stage: "resolve-identity",
    });
  });
});
