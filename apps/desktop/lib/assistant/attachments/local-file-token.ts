import {
  markdownSegments,
  mapMarkdownText,
  splitMarkdownTokens,
} from "@/lib/assistant/markdown-literals";

/** Stable client-only token used to render a local ordinary attachment inline. */
const LOCAL_FILE_TOKEN = /\[\[cosir-file:([^\]]+)\]\]/g;

/** 图片附件的受控 locator 前缀（与后端 `cosir-attachment://<id>` 契约一致）。 */
export const LOCAL_IMAGE_LOCATOR_PREFIX = "cosir-attachment://";

/** 普通文件附件的受控 locator 前缀：`cosir-local-file:<id>`，id 指向本机附件登记（格式校验见 snapshot-validation）。 */
const LOCAL_FILE_LOCATOR_PREFIX = "cosir-local-file:";

export function localFileLocator(id: string): string {
  return `${LOCAL_FILE_LOCATOR_PREFIX}${id}`;
}

/** 取出普通文件 locator 里的登记 id；不是普通文件 locator（路径、远程地址、图片）时为 undefined。 */
export function localFileIdFromLocator(locator: string | undefined): string | undefined {
  return locator?.startsWith(LOCAL_FILE_LOCATOR_PREFIX) ? locator.slice(LOCAL_FILE_LOCATOR_PREFIX.length) : undefined;
}

export function localFileTokenIds(text: string): string[] {
  return markdownSegments(text)
    .filter((part) => !part.literal)
    .flatMap((part) =>
      [...part.text.matchAll(LOCAL_FILE_TOKEN)].map((match) => match[1]),
    );
}

/** 按位置切出普通文件 token 的 id；与后端 `build_user_input_parts` 的切分一致，供乐观消息对齐 parts 顺序。 */
export function splitLocalFileTokens(text: string) {
  return splitMarkdownTokens(text, LOCAL_FILE_TOKEN, (match) => match[1] ?? null);
}

/** 注释包裹或裸写形式的内联附件 token（含图片），用于从用户可见文本中剔除。 */
const INLINE_ATTACHMENT_TOKEN_PATTERN =
  /<!--\s*\[\[cosir-(?:file|image):[^\]]+\]\]\s*-->|\[\[cosir-(?:file|image):[^\]]+\]\]/g;

/**
 * 去掉内联附件 token，得到用户可见文本。
 *
 * 负责什么：剔除 `[[cosir-file:<id>]]`、`[[cosir-image:<id>]]` 及其注释包裹形式，并把因此
 * 产生的多余空白压成单空格。
 * 不负责什么：不截断长度、不判断附件是否真实存在、不修改任何事实。
 *
 * 参数:
 *     text: 可能含内联 token 的任意文本（消息原文、任务标题等）。
 *
 * 返回:
 *     剔除 token 并归一空白后的文本；若原文本只有 token 则返回空字符串。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function stripInlineAttachmentTokens(text: string): string {
  return mapMarkdownText(text, (part) =>
    part.replace(INLINE_ATTACHMENT_TOKEN_PATTERN, " "),
  )
    .replace(/\s+/g, " ")
    .trim();
}

/** 内联附件身份判定所需的最小视图：只读取附件 id 与 content 里的 locator。 */
export type InlineAttachmentIdentity = {
  id: string;
  content?: readonly { type: string; data?: string; image?: string }[];
};

/**
 * 提取附件在文本 token 中使用的 id。
 *
 * 为什么需要：同一个附件可能来自两个来源——canonical 消息投影（`id` 即 token id）与
 * assistant-ui 默认 edit core 的 lift 副本（`id` 是新生成的随机值，但 content 保留
 * locator）。两者必须归一到同一个 id，否则会重复加入附件或误判为失效。
 *
 * 参数:
 *     attachment: 任一来源的附件对象；只读取 `id` 与 content 中的受控 locator。
 *
 * 返回:
 *     普通文件取 `cosir-local-file:<id>` 的 id；图片取 `cosir-attachment://<id>` 的 id；
 *     两者都没有时退回附件自身 `id`。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function inlineAttachmentTokenId(
  attachment: InlineAttachmentIdentity,
): string {
  const fileContent = attachment.content?.find(
    (content) => content.type === "file",
  );
  const localFileId = localFileIdFromLocator(fileContent?.data);
  if (localFileId !== undefined) return localFileId;
  const imageContent = attachment.content?.find(
    (content) => content.type === "image",
  );
  if (imageContent?.image?.startsWith(LOCAL_IMAGE_LOCATOR_PREFIX)) {
    return imageContent.image.slice(LOCAL_IMAGE_LOCATOR_PREFIX.length);
  }
  return attachment.id;
}
