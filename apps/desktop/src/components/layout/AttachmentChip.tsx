/**
 * 单个附件芯片（AttachmentChip）。
 *
 * 单一职责：渲染一个 ``AttachmentRef`` 的语义化 chip——图标/标签按类型区分、
 * 非图片附件（file/directory/url）明确提示「Agent 会按需读取/抓取，不会立即上传全文」、
 * 提供移除按钮。不含任何附件状态管理逻辑（状态在 ``useAttachmentInput``）。
 *
 * 不负责边界：
 * - 不维护附件列表（增删由父组件经 ``useAttachmentInput`` 控制）。
 * - 不负责发送前校验（由 ``useAttachmentInput.validateForSend`` 完成）。
 *
 * @module components/layout/AttachmentChip
 */

import { FileText, FolderOpen, ImageIcon, LinkIcon, X } from "lucide-react";
import type { AttachmentKind, AttachmentRef } from "@shared/attachment";

/**
 * AttachmentChip 组件属性。
 */
export interface AttachmentChipProps {
  /** 待渲染的附件引用。 */
  attachment: AttachmentRef;
  /** 移除回调（按 ref 移除）。 */
  onRemove: (ref: string) => void;
}

/** 各附件类型的展示图标映射。 */
const KIND_ICON: Record<AttachmentKind, typeof ImageIcon> = {
  image: ImageIcon,
  file: FileText,
  directory: FolderOpen,
  url: LinkIcon,
};

/** 非图片附件的补充说明（file/directory/url 不会立即上传全文）。 */
const NON_IMAGE_HINT = "（Agent 按需读取，不立即上传）";

/**
 * 取附件展示名（路径最后一段；URL 取 host+path 末段；无法解析时回退原 ref）。
 *
 * @param ref - 附件引用路径或 URL。
 * @returns 面向用户的短标签。
 */
function attachmentLabel(ref: string): string {
  const normalized = ref.trim().replace(/\\/g, "/");
  const segments = normalized.split("/").filter(Boolean);
  const last = segments.length > 0 ? segments[segments.length - 1] : undefined;
  return last || normalized;
}

/**
 * 单个附件芯片。
 *
 * @param props - 组件属性（attachment / onRemove）。
 * @returns 渲染一个附件 chip。
 */
export function AttachmentChip({ attachment, onRemove }: AttachmentChipProps) {
  const Icon = KIND_ICON[attachment.kind];
  const label = attachmentLabel(attachment.ref);
  const isNonImage = attachment.kind !== "image";

  return (
    <span
      className="inline-flex max-w-56 items-center gap-1 rounded-md border border-border bg-muted/60 px-2 py-1 text-xs text-foreground"
      title={attachment.ref}
    >
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 truncate">{label}</span>
      {isNonImage && (
        <span className="shrink-0 text-xs leading-none text-muted-foreground">{NON_IMAGE_HINT}</span>
      )}
      <button
        type="button"
        aria-label={`移除附件 ${label}`}
        className="ml-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-background hover:text-foreground"
        onClick={() => onRemove(attachment.ref)}
        onMouseDown={(e) => e.preventDefault()}
      >
        <X className="h-3 w-3" />
      </button>
    </span>
  );
}
