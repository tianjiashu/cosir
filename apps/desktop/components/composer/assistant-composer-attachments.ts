import type { InlineComposerAttachment } from "@/components/composer/composer-document";
import type { ComposerFileProblem } from "@/components/composer/composer-external-files";
import { inlineAttachmentTokenId, type InlineAttachmentIdentity } from "@/lib/assistant/attachments/local-file-token";

type AssistantComposerAttachment = InlineAttachmentIdentity & {
  name: string;
  type: string;
  file?: File;
};

type AssistantComposerAttachmentPort<TAttachment extends AssistantComposerAttachment> = {
  getAttachments: () => readonly TAttachment[];
  addAttachment: (file: File) => Promise<void>;
};

export type AssistantComposerFile = {
  file: File;
  name: string;
  skipIfAttachmentIdExists?: string;
};

export type AssistantComposerAttachmentFailure = {
  file: AssistantComposerFile;
  stage: "add" | "resolve-identity";
  error?: unknown;
};

/**
 * 把文件接入 Assistant UI composer，并只为运行时确认存在的普通文件生成内联 token。
 * 文件选择器与粘贴/拖拽共用此边界，避免各入口对运行时附件身份和部分失败的处理漂移。
 */
export async function addFilesToAssistantComposer<TAttachment extends AssistantComposerAttachment>(
  files: readonly AssistantComposerFile[],
  port: AssistantComposerAttachmentPort<TAttachment>,
  onFailure: (failure: AssistantComposerAttachmentFailure) => void,
): Promise<{ attachments: InlineComposerAttachment[]; problems: ComposerFileProblem[] }> {
  const existingIds = new Set(port.getAttachments().map((attachment) => attachment.id));
  const insertedFiles: InlineComposerAttachment[] = [];
  const problems: ComposerFileProblem[] = [];

  for (const file of files) {
    if (file.skipIfAttachmentIdExists && existingIds.has(file.skipIfAttachmentIdExists)) continue;

    try {
      await port.addAttachment(file.file);
    } catch (error) {
      problems.push({ name: file.name, reason: "add-failed" });
      onFailure({ file, stage: "add", error });
      continue;
    }

    const added = port.getAttachments().find(
      (attachment) => attachment.file === file.file && !existingIds.has(attachment.id),
    );
    if (!added) {
      problems.push({ name: file.name, reason: "add-failed" });
      onFailure({ file, stage: "resolve-identity" });
      continue;
    }

    existingIds.add(added.id);
    if (added.type !== "image") {
      insertedFiles.push({
        id: added.id,
        name: added.name,
        kind: "file",
        tokenId: inlineAttachmentTokenId(added),
      });
    }
  }

  return { attachments: insertedFiles, problems };
}
