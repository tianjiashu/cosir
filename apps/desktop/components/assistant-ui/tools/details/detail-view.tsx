import type { ReactNode } from "react";
import { LinkIcon } from "lucide-react";
import type { CallPresentation } from "../call-view";
import { safeExternalUrl } from "../display-utils";
import { cn } from "@/lib/utils";

/** 展开体列表行的几何（搜索命中、目录条目、外链行）；语义样式由各视图自己加。 */
export const DETAIL_ROW_CLASS = "flex min-h-7 items-center gap-2 py-1 text-xs";

/** 内容型展开体（终端、动作步骤、截图、指令正文）的底面；列表型不用，直接对齐标题文字。 */
export function DetailSurface({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("bg-surface rounded-lg px-3 py-2 text-xs", className)}>{children}</div>;
}

export function DetailEmpty({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground text-xs">{children}</p>;
}

/**
 * 列表型展开体：判空、后端短提示（截断、部分成功）与列表外壳，各视图只给单行怎么画。
 * `rows` 是函数：大列表到首次展开才解析，调用行每次渲染都会解析视图，不能在这里付这笔开销。
 */
export function listDetail(
  data: Record<string, unknown>,
  rows: () => Record<string, unknown>[],
  empty: string,
  renderRow: (row: Record<string, unknown>, index: number) => ReactNode,
): NonNullable<CallPresentation["detail"]> {
  return {
    render: () => {
      const items = rows();
      if (items.length === 0) return <DetailEmpty>{empty}</DetailEmpty>;
      return (
        <div className="space-y-2">
          {typeof data.status_hint === "string" && <p className="text-muted-foreground text-xs">{data.status_hint}</p>}
          <ul className="text-xs">{items.map(renderRow)}</ul>
        </div>
      );
    },
  };
}

/**
 * 外链行：只有通过 `safeExternalUrl` 的地址才成为可点击链接，其余原样作为文本展示，
 * 不信任工具结果里的协议与主机。
 */
export function ExternalLinkRow({ rawUrl, title }: { rawUrl: string; title: string }) {
  const url = safeExternalUrl(rawUrl);
  return (
    <li className={DETAIL_ROW_CLASS}>
      <LinkIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
      {url
        ? <a href={url} target="_blank" rel="noreferrer" className="truncate hover:text-foreground" title={url}>{title}</a>
        : <span className="truncate" title={rawUrl}>{title}</span>}
    </li>
  );
}
