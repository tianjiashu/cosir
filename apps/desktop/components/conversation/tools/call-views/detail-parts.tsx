import type { ReactNode } from "react";
import { WebPageIcon } from "@/components/links/link-target-icon";
import { useOpenLink } from "@/components/links/use-open-link";
import { resolveLink } from "@/lib/links/resolve-link";
import type { CallPresentation } from "../call-view";
import { DetailBudget } from "../detail-budget";

/** 展开体列表行的几何（搜索命中、目录条目、外链行）；语义样式由各视图自己加。 */
export const DETAIL_ROW_CLASS = "flex min-h-7 items-center gap-2 py-1 text-xs";

/** 内容型展开体（动作步骤、截图、正文）的底面，内容受统一的高度上限；列表型不用，直接对齐标题文字。 */
export function DetailSurface({ className, children }: { className?: string; children: ReactNode }) {
  return <div className="bg-surface rounded-lg px-3 py-2 text-xs"><DetailBudget className={className}>{children}</DetailBudget></div>;
}

export function DetailEmpty({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground text-xs">{children}</p>;
}

/**
 * 列表型展开体：判空、后端短提示（截断、部分成功）、列表外壳（统一高度上限）与分页说明，各视图只给单行怎么画。
 * `rows` 是函数：大列表到首次展开才解析，调用行每次渲染都会解析视图，不能在这里付这笔开销。
 * 分页说明读展示数据的 `page`（`{offset, total}`，后端列表工具给）：这一页不是全部时写清范围与总数，
 * 用户不会把「这一页」当成「全部」。
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
      const range = pageRange(data.page, items.length);
      return (
        <div className="space-y-2">
          {typeof data.status_hint === "string" && <p className="text-muted-foreground text-xs">{data.status_hint}</p>}
          <DetailBudget><ul className="text-xs">{items.map(renderRow)}</ul></DetailBudget>
          {range && <p data-slot="list-page-range" className="text-muted-foreground text-xs">{range}</p>}
        </div>
      );
    },
  };
}

/** 这一页不是全部时的范围说明，如「第 51–100 条 · 共 1000 条」；没有 `page` 或这一页就是全部时为 null。 */
export function pageRange(page: unknown, shown: number): string | null {
  const { offset, total } = (page ?? {}) as { offset?: unknown; total?: unknown };
  if (typeof offset !== "number" || typeof total !== "number" || shown === 0) return null;
  if (offset === 0 && shown >= total) return null;
  return `第 ${offset + 1}–${offset + shown} 条 · 共 ${total} 条`;
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
