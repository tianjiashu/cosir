import { ATTACHMENT_ASSET_ID } from "@/lib/assistant/attachments/attachment-locator";
import { readBrowserPageDisplay } from "./browser-page-display";
import { readImageReadDisplay } from "./image-read-display";

/** 一次调用产出、可以放进汇总的图片：附件 asset id 与替代文本（读到的图片是路径，截图是网页标题）。 */
export type CallImage = { assetId: string; alt: string };

const NONE: readonly CallImage[] = [];

/**
 * 按展示类型列出一次调用的图片，汇总行与调用详情看到的是同一批图。
 *
 * 只认带图片的展示类型（`image-read` 读到的图、`browser-page` 的截图、`mcp-tool-result` 调用返回的图），
 * 按 `kind` 声明、不按工具名分支；失败的调用没有展示数据，自然不贡献图片。
 */
export function callImages(displayData: unknown): readonly CallImage[] {
  const kind = (displayData as { kind?: unknown } | null)?.kind;
  if (kind === "image-read") return readImageReadDisplay(displayData)?.images.map((image) => ({ assetId: image.assetId, alt: image.path })) ?? NONE;
  if (kind === "browser-page") {
    const page = readBrowserPageDisplay(displayData);
    return page?.screenshot ? [{ assetId: page.screenshot, alt: page.title || page.url }] : NONE;
  }
  if (kind === "mcp-tool-result") {
    const data = displayData as { images?: unknown; server_name?: unknown; tool_name?: unknown };
    const alt = `${String(data.server_name ?? "MCP")} / ${String(data.tool_name ?? "工具")}`;
    return Array.isArray(data.images)
      ? data.images.filter((id): id is string => typeof id === "string" && ATTACHMENT_ASSET_ID.test(id)).map((assetId) => ({ assetId, alt }))
      : NONE;
  }
  return NONE;
}
