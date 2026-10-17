import { UserFacingError } from "@/lib/errors/user-facing-error";

export type LocalAttachmentMetadata = {
  id: string;
  /** 历史消息只带展示信息；路径由编辑重跑边界交给后端恢复。 */
  path?: string;
  name: string;
  contentType: string;
  kind: "image" | "file";
};

/** A safe, user-actionable error for a local attachment that can no longer be resolved. */
export class LocalAttachmentUnavailableError extends UserFacingError {
  constructor() {
    super("附件已失效，请重新选择附件");
    this.name = "LocalAttachmentUnavailableError";
  }
}

type NativeAttachmentMetadata = LocalAttachmentMetadata & { path: string };

const metadataByFile = new WeakMap<File, NativeAttachmentMetadata>();
const metadataById = new Map<string, LocalAttachmentMetadata>();

/** Associate a native-picker path with the File object consumed by assistant-ui. */
export function registerLocalAttachment(file: File, metadata: NativeAttachmentMetadata): File {
  metadataByFile.set(file, metadata);
  metadataById.set(metadata.id, metadata);
  return file;
}

/** 保存历史普通文件的展示元数据，不覆盖本机选择器已经登记的真实路径。 */
export function registerInlineFileMetadata(metadata: Pick<LocalAttachmentMetadata, "id" | "name" | "contentType">): void {
  if (!metadataById.has(metadata.id)) metadataById.set(metadata.id, { ...metadata, kind: "file" });
}

export function getLocalAttachment(file: File): NativeAttachmentMetadata | undefined {
  return metadataByFile.get(file);
}

export function getLocalAttachmentById(id: string): LocalAttachmentMetadata | undefined {
  return metadataById.get(id);
}

export const LOCAL_FILE_DATA_PREFIX = "cosir-local-file:";
