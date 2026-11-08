import type { ReactNode } from "react";
import { WebPageIcon } from "@/components/links/link-target-icon";
import { useOpenLink } from "@/components/links/use-open-link";
import { resolveLink } from "@/lib/links/resolve-link";
import { cn } from "@/lib/utils";
import type { CallPresentation } from "../call-view";

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
 * 外链行：工具结果不可信，只有解析成网页地址的才可点击，并和其他链接一样经 `useOpenLink` 确认后打开；
 * 其余原样作为文本展示。
 */
export function ExternalLinkRow({ rawUrl, title }: { rawUrl: string; title: string }) {
  const links = useOpenLink("tool");
  const target = resolveLink(rawUrl, { workspaceRoot: null });
  return (
    <li className={DETAIL_ROW_CLASS}>
      <WebPageIcon url={rawUrl} className="size-3.5" />
      {target?.kind === "web"
        ? (
          <a
            href={target.url}
            className="truncate hover:text-foreground"
            title={target.url}
            onClick={(event) => {
              event.preventDefault();
              links.open(target);
            }}
          >{title}</a>
        )
        : <span className="truncate" title={rawUrl}>{title}</span>}
    </li>
  );
}
