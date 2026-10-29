import type { ReactNode } from "react";
import { LinkIcon } from "lucide-react";
import type { TransportToolStatus } from "@/lib/assistant/contract";
import { TOOL_DETAIL_ROW_CLASS } from "../../elements/tool-layout-tokens";
import { safeExternalUrl } from "../types";

/**
 * 一种成功态 `display_data.kind` 在低噪音工具卡里的投影。
 *
 * `summary` 是折叠行上的一句话；`body` 返回展开内容，返回 null 表示这份载荷没有值得
 * 展开的内容（卡片因此渲染为静态行）。二者都是纯函数，只读后端已投影的展示数据，
 * 失败与取消由 `ToolCardShell` 统一处理，这里只会拿到成功或进行中的载荷。
 */
export type DetailView = {
  /**
   * 可展开卡片的整行是折叠按钮，摘要里不能再放可点击元素；只有没有展开内容的
   * 静态行（如读文件）才在摘要里放路径链接。
   */
  summary: (data: Record<string, unknown>, status: TransportToolStatus) => ReactNode;
  body?: (data: Record<string, unknown>) => ReactNode;
};

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
