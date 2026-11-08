import { resolveLink } from "@/lib/links/resolve-link";
import type { CallView } from "../call-view";
import { readArray, readRecords } from "../display-data-readers";
import { ExternalLinkRow, listDetail } from "./detail-parts";

function siteOf(value: string): string {
  const target = resolveLink(value, { workspaceRoot: null });
  return (target?.kind === "web" && target.publicHost) || value;
}

const withUrl = (value: unknown) => readRecords(value).filter((item) => typeof item.url === "string" && item.url);

/** `web-search-results`：只展示标题链接，摘要正文属于模型上下文，不进入卡片。 */
export const webSearchView: CallView = (data) => {
  return {
    meta: `${readArray(data.results).length} 个结果`,
    detail: listDetail(data, () => withUrl(data.results), "未找到匹配", (item, index) => {
      const rawUrl = item.url as string;
      return <ExternalLinkRow key={`${rawUrl}-${index}`} rawUrl={rawUrl} title={typeof item.title === "string" && item.title ? item.title : rawUrl} />;
    }),
  };
};

/** `web-extract-urls`：只列出抓取了哪些站点，抓取正文不进入卡片；目标已在行上，这里不再写元信息。 */
export const webExtractView: CallView = (data) => {
  return {
    detail: listDetail(data, () => withUrl(data.urls), "未找到匹配", (item, index) => {
      const rawUrl = item.url as string;
      return <ExternalLinkRow key={`${rawUrl}-${index}`} rawUrl={rawUrl} title={siteOf(rawUrl)} />;
    }),
  };
};
