import type { ReactNode } from "react";
import { LinkIcon } from "lucide-react";
import { TOOL_DETAIL_ROW_CLASS } from "../../elements/tool-layout-tokens";
import { safeExternalUrl } from "../display-utils";
import type { CallView } from "../call-view";

/**
 * 走通用列表视图的 kind 用同一个 `CallView` 契约：`meta` 是行上的一句摘要，`body` 是展开列表。
 * 可展开行整行是折叠按钮，摘要里不能再放可点击元素；只有没有展开内容的静态行（如读文件）才在摘要里放路径链接。
 */
export type DetailView = CallView;

export function DetailEmpty({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground text-xs">{children}</p>;
}

/** 列表上方的后端短提示（如截断、部分成功），没有时不占位。 */
export function DetailHint({ data }: { data: Record<string, unknown> }) {
  return typeof data.status_hint === "string" ? <p className="text-muted-foreground text-xs">{data.status_hint}</p> : null;
}

/**
 * 外链行：只有通过 `safeExternalUrl` 的地址才成为可点击链接，其余原样作为文本展示，
 * 不信任工具结果里的协议与主机。
 */
export function ExternalLinkRow({ rawUrl, title, icon }: { rawUrl: string; title: string; icon?: ReactNode }) {
  const url = safeExternalUrl(rawUrl);
  return (
    <li className={TOOL_DETAIL_ROW_CLASS}>
      {icon ?? <LinkIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />}
      {url
        ? <a href={url} target="_blank" rel="noreferrer" className="truncate hover:text-foreground" title={url}>{title}</a>
        : <span className="truncate" title={rawUrl}>{title}</span>}
    </li>
  );
}

export function readArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function readString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}
