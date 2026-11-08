import { UserFacingError } from "@/lib/errors/user-facing-error";

/**
 * 输入框里普通文件附件（含文件夹）的元数据，按草稿 token 里的 id 查找。
 *
 * 普通文件不上传内容：发送时只把本机路径作为附件描述交给后端，由后端读取。图片走另一条路
 * （按字节上传，见 image-attachment-adapter），不在这里登记。
 */
export type LocalAttachmentMetadata = {
  id: string;
  /** 本机路径：新选的文件由宿主取得，放回输入框的历史附件取自快照（见 {@link restoreLocalFile}）。 */
  path: string;
  name: string;
  contentType: string;
};

/** 文件夹作为一个普通附件时使用的类型标记；后端按需浏览目录，前端不展开。 */
export const DIRECTORY_CONTENT_TYPE = "application/x-directory";

/** A safe, user-actionable error for a local attachment that can no longer be resolved. */
export class LocalAttachmentUnavailableError extends UserFacingError {
  constructor() {
    super("附件已失效，请重新选择附件");
    this.name = "LocalAttachmentUnavailableError";
  }
}

const metadataById = new Map<string, LocalAttachmentMetadata>();

/** 登记用户刚选择、粘贴或拖入的本机文件，返回写进草稿 token 的 id。 */
export function registerLocalFile(metadata: Omit<LocalAttachmentMetadata, "id">): string {
  const id = crypto.randomUUID();
  metadataById.set(id, { id, ...metadata });
  return id;
}

/**
 * 按原 id 登记快照里的历史附件：放回输入框的正文 token 沿用原 id，页面重载后登记表为空，
 * 只能从快照恢复。同 id 的登记来自同一次选择，覆盖无妨。
 */
export function restoreLocalFile(metadata: LocalAttachmentMetadata): void {
  metadataById.set(metadata.id, metadata);
}

export function getLocalAttachmentById(id: string): LocalAttachmentMetadata | undefined {
  return metadataById.get(id);
}

