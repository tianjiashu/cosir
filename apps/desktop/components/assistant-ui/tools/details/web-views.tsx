import { toolStatusLabel } from "../tool-status";
import { asRecord, safeExternalUrl } from "../types";
import { DetailEmpty, DetailHint, ExternalLinkRow, readArray, readString, type DetailView } from "./detail-view";

function siteOf(value: unknown): string {
  if (typeof value !== "string" || !value) return "网站未知";
  const safeUrl = safeExternalUrl(value);
  return safeUrl ? new URL(safeUrl).hostname : value;
}

/** `web-search-results`：只展示标题链接，摘要正文属于模型上下文，不进入卡片。 */
export const webSearchView: DetailView = {
  summary: (data) => `${readString(data.query, "搜索")} · ${readArray(data.results).length} 个结果`,
  body: (data) => {
    const results = readArray(data.results).map(asRecord).filter((item) => typeof item.url === "string" && item.url);
    if (results.length === 0) return <DetailEmpty>未找到匹配</DetailEmpty>;
    return (
      <div className="space-y-2">
        <DetailHint data={data} />
        <ul className="text-xs">
          {results.map((item, index) => {
            const rawUrl = item.url as string;
            const title = typeof item.title === "string" && item.title ? item.title : rawUrl;
            return <ExternalLinkRow key={`${rawUrl}-${index}`} rawUrl={rawUrl} title={title} />;
          })}
        </ul>
      </div>
    );
  },
};

/** `web-extract-urls`：只列出抓取了哪些站点，抓取正文不进入卡片。 */
export const webExtractView: DetailView = {
  summary: (data, status) => {
    const urls = readArray(data.urls);
    const firstUrl = urls.length > 0 ? asRecord(urls[0]).url : undefined;
    const target = urls.length > 1 ? `${siteOf(firstUrl)} 等 ${urls.length} 个网站` : siteOf(firstUrl);
    return `${target} · ${typeof data.status_hint === "string" ? data.status_hint : toolStatusLabel(status)}`;
  },
  body: (data) => {
    const urls = readArray(data.urls).map(asRecord).filter((item) => typeof item.url === "string" && item.url);
    if (urls.length === 0) return <DetailEmpty>未找到匹配</DetailEmpty>;
    return (
      <ul className="text-xs">
        {urls.map((item, index) => {
          const rawUrl = item.url as string;
          const url = safeExternalUrl(rawUrl);
          const parsed = url ? new URL(url) : null;
          const icon = parsed ? (
            <img
              src={`${parsed.origin}/favicon.ico`}
              alt=""
              className="size-3.5 shrink-0"
              onError={(event) => { event.currentTarget.style.display = "none"; }}
            />
          ) : undefined;
          return <ExternalLinkRow key={`${rawUrl}-${index}`} rawUrl={rawUrl} title={parsed?.hostname ?? rawUrl} icon={icon} />;
        })}
      </ul>
    );
  },
};
