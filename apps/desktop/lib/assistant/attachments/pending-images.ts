import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { TraceId } from "@/lib/logging/trace-id";
import { LOCAL_IMAGE_LOCATOR_PREFIX } from "./attachment-locator";
import { uploadAttachment, type UploadedAttachment } from "./attachment-upload";

type MessagePart = UserAddMessageCommand["message"]["parts"][number];

/**
 * 已点发送、还没上传的图片。
 *
 * 点发送时消息整份离开输入框，交给 Session 级的提交（`SessionTransportConnection.submit`）：输入框立刻空出来，
 * 图片以 object URL 显示在乐观消息里，提交在 POST 前才经 {@link uploadPendingImages} 上传、换成附件 locator。
 * 上传中的消息因此只有提交这一个持有者，切走 Session、输入框随页面卸载都不影响它；上传失败与 POST 被拒是同一种
 * 提交失败，内容经 `recoverRejectedCommands` 回到输入框（图片仍是这个 object URL，重发时照样能找回原文件）。
 *
 * object URL 就是登记的键：命令里的图片 part、乐观消息的显示地址、放回输入框的附件都是同一个字符串。后端收下、
 * 乐观消息被正式消息替换（或连接被回收）后由连接 {@link releasePendingImages}；提交失败时不释放，内容在输入框里等着重发。
 * 已知边界：放回输入框后又被用户移除的图片不会释放，一张图的内存留到页面关闭。
 */
const files = new Map<string, File>();
/** 同一工作区里相同内容的图片只上传一次（同一条消息里贴了两次、或重发同一张）；失败的不留在缓存里。 */
const uploads = new Map<string, Promise<UploadedAttachment>>();

/** 登记一张待上传的图片，返回它在命令与乐观消息里的地址。 */
export function holdPendingImage(file: File): string {
  const url = URL.createObjectURL(file);
  files.set(url, file);
  return url;
}

/**
 * 把消息里的待上传图片换成附件 locator，其余 part 原样返回（新数组，不改传入的命令）。
 *
 * 异常:
 *     UserFacingError: 有图片上传失败（含超时）；消息不应发出，由提交方按提交失败收回乐观消息、放回输入框。
 */
export async function uploadPendingImages(
  workspaceId: number | null,
  parts: readonly MessagePart[],
  traceId: TraceId,
): Promise<MessagePart[]> {
  return Promise.all(parts.map(async (part) => {
    const file = part.type === "image" ? files.get(part.image) : undefined;
    if (file === undefined) return part;
    // 没有工作区的 Session 没有附件适配器，命令里不会出现待上传图片。
    const uploaded = await upload(workspaceId!, file, traceId);
    return { type: "image" as const, image: `${LOCAL_IMAGE_LOCATOR_PREFIX}${uploaded.id}` };
  }));
}

/** 释放消息里的待上传图片（后端已收下、乐观消息已被替换）；不是待上传图片的 part 不受影响。 */
export function releasePendingImages(parts: readonly MessagePart[]): void {
  for (const part of parts) {
    if (part.type !== "image" || !files.delete(part.image)) continue;
    URL.revokeObjectURL(part.image);
  }
}

async function upload(workspaceId: number, file: File, traceId: TraceId): Promise<UploadedAttachment> {
  const key = `${workspaceId}:${await digest(file)}`;
  let pending = uploads.get(key);
  if (!pending) {
    pending = uploadAttachment(workspaceId, file, traceId);
    uploads.set(key, pending);
    void pending.catch(() => {
      if (uploads.get(key) === pending) uploads.delete(key);
    });
  }
  try {
    return await pending;
  } catch (error) {
    void frontendLog("ERROR", "composer_attachment_upload_failed", "发送前图片上传失败，本条消息未发出", {
      traceId,
      data: { workspaceId, name: file.name, size: file.size },
      error,
    });
    throw new UserFacingError(`图片「${file.name}」上传失败，消息未发出：${userErrorMessage(error, "请重试")}`);
  }
}

async function digest(file: File): Promise<string> {
  const bytes = await file.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((value) => value.toString(16).padStart(2, "0")).join("");
}
