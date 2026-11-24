import { userErrorMessage } from "@/lib/errors/user-error-message";
import { createContext, useCallback, useContext, useState, type ComponentProps, type RefObject } from "react";
import { useDomSelectionSource, type TextSelectionState } from "@/components/text-selection/use-text-selection";
import type { QuoteCapsule } from "@/lib/assistant/capsules";
import { useQuery } from "@tanstack/react-query";
import { useWatchedPaths } from "@/lib/workspace-files/file-watch-store";
import type { ExtraProps } from "streamdown";
import { MarkdownLinkScope, useMarkdownLinkScope } from "@/components/links/markdown-links";
import { useOpenLink } from "@/components/links/use-open-link";
import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { parentPath } from "@/lib/workspace-files/workspace-files-store";
import { useFileResourceSrc, useResourceLoadFailure } from "@/lib/workspace-files/resources";
import { isUnreadableImage } from "@/lib/api/workspace-files";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";
import type { ReadingAnchor } from "@/lib/reading/reading-anchor";
import { useRenderedAnchor } from "@/lib/reading/rendered-anchor";

function LocalImage({
  id,
  path,
  alt,
}: {
  id: number;
  path: string;
  alt?: string;
}) {
  useWatchedPaths(id, [path]);
  const metadata = useQuery({
    ...fileInfoQueryOptions(id, path),
  });
  const src = useFileResourceSrc(id, path, metadata.data?.version);
  const failure = useResourceLoadFailure(id, path, src);
  if (metadata.error)
    // 文档引用的图片不在是常见的文档过期，不是故障：灰字说明即可，文件恢复时随文件监听自动出现。
    return fileReadProblem(metadata.error) === "missing-file" ? (
      <span className="text-muted-foreground">图片不存在：{path}</span>
    ) : (
      <span role="alert">
        {userErrorMessage(metadata.error, "图片读取失败")}
      </span>
    );
  if (failure.failed || (metadata.data !== undefined && isUnreadableImage(metadata.data)))
    return <span role="alert">无法预览这张图片：{path}</span>;
  return src ? (
    <img src={src} alt={alt ?? ""} className="max-w-full" onError={() => void failure.onError()} />
  ) : (
    <span>图片加载中…</span>
  );
}
/** 预览所属的工作区：图片按工作区读本地文件。 */
const PreviewWorkspaceContext = createContext<number | null>(null);

/**
 * 文档里的图片：按与链接相同的规则解析（同一个链接作用域），工作区图片读本地文件显示，外部图片不自动加载。
 * 模块级、身份恒定，会变的工作区与打开器从 context 读（理由见 `StaticMarkdown`）。
 */
function DocumentImage({ src, alt, node }: ComponentProps<"img"> & ExtraProps) {
  void node;
  const { opener, documentDir } = useMarkdownLinkScope();
  const workspaceId = useContext(PreviewWorkspaceContext)!;
  const target = typeof src === "string" ? opener.resolve(src, documentDir) : null;
  return target?.kind === "workspace" && !target.directory ? (
    <LocalImage id={workspaceId} path={target.path} alt={alt} />
  ) : (
    <span className="text-muted-foreground">
      外部图片：{alt || "未自动加载"}
    </span>
  );
}

const PREVIEW_COMPONENTS = { img: DocumentImage } as const;

export function MarkdownPreview({
  hostRef: host,
  text,
  workspaceId,
  path,
  anchor,
  onAnchorChange,
  selection,
  resolveSelection,
}: {
  /** 滚动容器；阅读器经它做就地查找与大纲跳转。 */
  hostRef: RefObject<HTMLDivElement | null>;
  text: string;
  workspaceId: number;
  path: string;
  /** 打开时恢复到的阅读位置（与源码视图共用）；之后内容异步变高由浏览器的滚动锚定保持。 */
  anchor?: ReadingAnchor;
  onAnchorChange: (anchor: ReadingAnchor) => void;
  /** 选区来源：渲染视图的选区经 `resolveSelection` 换成摘录，交给阅读器的选区浮条。 */
  selection: TextSelectionState;
  resolveSelection: (range: Range, host: HTMLElement) => QuoteCapsule | null;
}) {
  // 选区来源要元素本身（挂载后才有），`host` 这个 ref 仍交给查找与大纲。
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const attach = useCallback((node: HTMLDivElement | null) => {
    host.current = node;
    setElement(node);
  }, [host]);
  const resolve = useCallback((range: Range) => (element ? resolveSelection(range, element) : null), [element, resolveSelection]);
  useDomSelectionSource(element, selection, resolve);
  useRenderedAnchor(host, anchor, onAnchorChange);
  const documentDir = parentPath(path);
  const opener = useOpenLink("document");
  return (
    <div
      ref={attach}
      data-slot="markdown-preview"
      className="h-full overflow-auto px-6 py-5"
    >
      {/* 限制行宽：面板铺满时正文仍保持易读的行长。 */}
      <div className="mx-auto max-w-3xl">
        <PreviewWorkspaceContext.Provider value={workspaceId}>
          <MarkdownLinkScope opener={opener} documentDir={documentDir}>
            <StaticMarkdown components={PREVIEW_COMPONENTS}>{text}</StaticMarkdown>
          </MarkdownLinkScope>
        </PreviewWorkspaceContext.Provider>
      </div>
    </div>
  );
}
