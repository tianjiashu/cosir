import type { CompleteAttachment } from "@assistant-ui/core";

/**
 * The UI working document for an editable user message.
 *
 * Ordinary files are inline blocks because their token has a meaningful text
 * position. Images deliberately live in a separate preview surface and are
 * therefore not represented by inline text tokens. This is a UI projection;
 * the backend canonical message remains the source of truth.
 */
export type EditableUserDocument = {
  text: string;
  inlineFiles: readonly CompleteAttachment[];
  previewImages: readonly CompleteAttachment[];
};

/** Build the one composer-facing projection from ordered text and attachments. */
export function createEditableUserDocument(
  text: string,
  attachments: readonly CompleteAttachment[],
): EditableUserDocument {
  const inlineFiles: CompleteAttachment[] = [];
  const previewImages: CompleteAttachment[] = [];
  const seen = new Set<string>();

  for (const attachment of attachments) {
    if (seen.has(attachment.id)) continue;
    seen.add(attachment.id);
    if (attachment.type === "image") previewImages.push(attachment);
    else inlineFiles.push(attachment);
  }

  return { text, inlineFiles, previewImages };
}

/** Return the exact ordered attachments that the composer must hydrate. */
export function editableDocumentAttachments(document: EditableUserDocument): readonly CompleteAttachment[] {
  return [...document.inlineFiles, ...document.previewImages];
}

