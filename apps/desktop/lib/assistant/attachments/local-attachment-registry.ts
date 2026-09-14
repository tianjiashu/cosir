export type LocalAttachmentMetadata = {
  id: string;
  path: string;
  name: string;
  contentType: string;
  kind: "image" | "file";
};

/** A safe, user-actionable error for a local attachment that can no longer be resolved. */
export class LocalAttachmentUnavailableError extends Error {
  constructor() {
    super("附件已失效，请重新选择附件");
    this.name = "LocalAttachmentUnavailableError";
  }
}

const metadataByFile = new WeakMap<File, LocalAttachmentMetadata>();
const metadataById = new Map<string, LocalAttachmentMetadata>();

/** Associate a native-picker path with the File object consumed by assistant-ui. */
export function registerLocalAttachment(file: File, metadata: LocalAttachmentMetadata): File {
  metadataByFile.set(file, metadata);
  metadataById.set(metadata.id, metadata);
  return file;
}

export function getLocalAttachment(file: File): LocalAttachmentMetadata | undefined {
  return metadataByFile.get(file);
}

export function getLocalAttachmentById(id: string): LocalAttachmentMetadata | undefined {
  return metadataById.get(id);
}

export const LOCAL_FILE_DATA_PREFIX = "cosir-local-file:";
