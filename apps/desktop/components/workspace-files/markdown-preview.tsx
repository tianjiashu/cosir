import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useMemo, useLayoutEffect, type ComponentProps, type RefObject } from "react";
import { useDomSelection } from "@/hooks/use-dom-selection";
import { useQuery } from "@tanstack/react-query";
import { useResourceWatch } from "@/lib/workspace-files/resource-watch";
import type { ExtraProps } from "streamdown";
import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { useOpenLink } from "@/components/links/use-open-link";
import { StaticMarkdown } from "@/components/static-markdown";
import { parentPath } from "@/lib/workspace-files/store";
import { useImageResource } from "@/lib/workspace-files/resources";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";

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
    ...fileInfoQueryOptions(id, path),
  });
  const image = useImageResource(id, path, metadata.data?.version);
  if (metadata.error)
    // 文档引用的图片不在是常见的文档过期，不是故障：灰字说明即可，文件恢复时随文件监听自动出现。
    return fileReadProblem(metadata.error) === "missing-file" ? (
      <span className="text-muted-foreground">图片不存在：{path}</span>
    ) : (
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
  hostRef: host,
  text,
  workspaceId,
  path,
  scrollTop,
  onScroll,
  onSelectText,
}: {
  /** 滚动容器；阅读器经它做就地查找与大纲跳转。 */
  hostRef: RefObject<HTMLDivElement | null>;
  text: string;
  workspaceId: number;
  path: string;
  scrollTop: number;
  onScroll: (top: number) => void;
  onSelectText?: (text: string | null) => void;
}) {
  useDomSelection(host, (value) => onSelectText?.(value));
  useLayoutEffect(() => {
    if (host.current) host.current.scrollTop = scrollTop;
  }, []);
  const documentDir = parentPath(path);
  const links = useMarkdownLinkComponents("document", { documentDir });
  const opener = useOpenLink("document");
  const components = useMemo(
    () => ({
      ...links,
      // 文档里的工作区图片按与链接相同的规则解析，读本地文件显示；外部图片不自动加载。
      img: ({ src, alt, node }: ComponentProps<"img"> & ExtraProps) => {
        void node;
        const target = typeof src === "string" ? opener.resolve(src, documentDir) : null;
        return target?.kind === "workspace" && !target.directory ? (
          <LocalImage id={workspaceId} path={target.path} alt={alt} />
        ) : (
          <span className="text-muted-foreground">
            外部图片：{alt || "未自动加载"}
          </span>
        );
      },
    }),
    [links, opener, documentDir, workspaceId],
  );
  return (
    <div
      ref={host}
      onScroll={(event) => onScroll(event.currentTarget.scrollTop)}
      className="h-full overflow-auto px-6 py-5"
    >
      {/* 限制行宽：面板铺满时正文仍保持易读的行长。 */}
      <div className="aui-md mx-auto max-w-3xl">
        <StaticMarkdown components={components}>{text}</StaticMarkdown>
      </div>
    </div>
  );
}
