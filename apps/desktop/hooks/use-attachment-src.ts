"use client";

import { useEffect, useState } from "react";
import { useAuiState } from "@assistant-ui/react";
import { useShallow } from "zustand/react/shallow";
import { getApiBaseUrl } from "@/lib/http/client";

export const resolveTransportImageSrc = (
  src: string,
  taskId?: number,
): string | undefined => {
  const locator = src.match(/^cosir-attachment:\/\/([0-9a-f]{64})$/)?.[1];
  if (locator && taskId !== undefined) {
    return `${getApiBaseUrl()}/tasks/${taskId}/attachments/${locator}/content`;
  }
  return /^(?:data:|blob:|https?:\/\/)/i.test(src) ? src : undefined;
};

const useFileSrc = (file: File | undefined) => {
  const [entry, setEntry] = useState<{ file: File; url: string } | undefined>(
    undefined,
  );

  useEffect(() => {
    if (!file) return;
    const objectUrl = URL.createObjectURL(file);
    const timer = window.setTimeout(() => {
      setEntry({ file, url: objectUrl });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      URL.revokeObjectURL(objectUrl);
    };
  }, [file]);

  return file && entry && entry.file === file ? entry.url : undefined;
};

export const useAttachmentSrc = (taskId?: number) => {
  const { file, src } = useAuiState(
    useShallow((s): { file?: File; src?: string } => {
      if (s.attachment.type !== "image") return {};
      if (s.attachment.file) return { file: s.attachment.file };
      const src = s.attachment.content?.filter((c) => c.type === "image")[0]
        ?.image;
      if (!src) return {};
      return { src };
    }),
  );

  return useFileSrc(file) ?? (src ? resolveTransportImageSrc(src, taskId) : undefined);
};
