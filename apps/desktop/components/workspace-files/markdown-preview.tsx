import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useMemo, useLayoutEffect, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { getFileInfo } from "@/lib/api/workspace-files";
import { queryKeys } from "@/lib/query/query-keys";
import { normalizeWorkspaceUrls } from "@/lib/workspace-files/markdown-urls";
import { useResourceWatch } from "@/lib/workspace-files/resource-watch";
import type { Components } from "react-markdown";
import { Streamdown, defaultRehypePlugins } from "streamdown";
import { markdownPlugins, useMermaidOptions } from "@/components/markdown-rendering-config";
import { parentPath } from "@/lib/workspace-files/store";
import { useImageResource } from "@/lib/workspace-files/resources";

const workspaceRehypePlugins = [
  defaultRehypePlugins.raw,
  defaultRehypePlugins.sanitize,
  normalizeWorkspaceUrls,
  defaultRehypePlugins.harden,
];

function resolveDocumentLink(
  document: string,
  href: string,
): string | null {
  if (
    /^[a-z][a-z\d+.-]*:/i.test(href) ||
    href.startsWith("//") ||
    href.startsWith("#")
  )
    return null;
  const parts = (href.startsWith("/") ? "" : `${parentPath(document)}/`).split(
    "/",
  );
  try {
    for (const part of decodeURIComponent(href.split(/[?#]/)[0]).split("/")) {
      if (part === "..") {
        if (!parts.filter(Boolean).length) return null;
        while (parts.at(-1) === "") parts.pop();
        parts.pop();
      } else if (part && part !== ".") parts.push(part);
    }
    return parts.filter(Boolean).join("/");
  } catch {
    return null;
  }
}
function LocalImage({
  id,
  path,
  alt,
}: {
  id: number;
  path: string;
  alt?: string;
}) {
  useResourceWatch(path);
  const metadata = useQuery({
    queryKey: queryKeys.fileInfo(id, path),
    queryFn: ({ signal }) => getFileInfo(id, path, signal),
  });
  const image = useImageResource(id, path, metadata.data?.version);
  if (metadata.error)
    return (
      <span role="alert">
        {userErrorMessage(metadata.error, "图片读取失败")}
      </span>
    );
  return image.url ? (
    <img src={image.url} alt={alt ?? ""} className="max-w-full" />
  ) : (
    <span>{image.error ?? "图片加载中…"}</span>
  );
}
export function MarkdownPreview({
  text,
  workspaceId,
  path,
  open,
  scrollTop,
  onScroll,
  onSelectText,
}: {
  text: string;
  workspaceId: number;
  path: string;
  open: (path: string) => void;
  scrollTop: number;
  onScroll: (top: number) => void;
  onSelectText?: (text: string | null) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const mermaidOptions = useMermaidOptions();
  useEffect(() => {
    const onChange = () => {
      const selection = window.getSelection();
      if (
        selection?.anchorNode &&
        selection.focusNode &&
        host.current?.contains(selection.anchorNode) &&
        host.current.contains(selection.focusNode)
      )
        onSelectText?.(selection.toString() || null);
    };
    document.addEventListener("selectionchange", onChange);
    return () => document.removeEventListener("selectionchange", onChange);
  }, [onSelectText]);
  useLayoutEffect(() => {
    if (host.current) host.current.scrollTop = scrollTop;
  }, []);
  const components = useMemo<Components>(
    () => ({
      a: ({ href, children }) => {
        const relative = href ? resolveDocumentLink(path, href) : null;
        return relative !== null ? (
          <button
            className="text-primary underline"
            onClick={() => open(relative)}
          >
            {children}
          </button>
        ) : (
          <a href={href} target="_blank" rel="noreferrer">
            {children}
          </a>
        );
      },
      img: ({ src, alt }) => {
        const relative =
          typeof src === "string" ? resolveDocumentLink(path, src) : null;
        return relative !== null ? (
          <LocalImage id={workspaceId} path={relative} alt={alt} />
        ) : (
          <span className="text-muted-foreground">
            外部图片：{alt || "未自动加载"}
          </span>
        );
      },
    }),
    [path, workspaceId, open],
  );
  return (
    <div
      ref={host}
      onMouseUp={() => {
        const selection = window.getSelection();
        if (
          selection?.anchorNode &&
          selection.focusNode &&
          host.current?.contains(selection.anchorNode) &&
          host.current.contains(selection.focusNode)
        )
          onSelectText?.(selection.toString() || null);
      }}
      onScroll={(event) => onScroll(event.currentTarget.scrollTop)}
      className="aui-md h-full overflow-auto p-5"
    >
      <Streamdown
        mode="static"
        plugins={markdownPlugins}
        rehypePlugins={workspaceRehypePlugins}
        shikiTheme={["github-light", "github-dark"]}
        linkSafety={{ enabled: true }}
        mermaid={mermaidOptions}
        components={components}
      >
        {text}
      </Streamdown>
    </div>
  );
}
