import { useMemo } from "react";
import { useAttachmentWorkspaceId } from "@/components/conversation/thread/attachment-context";
import { ImageStrip } from "@/components/image/image-strip";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import type { CallView } from "../call-view";
import { readArray, readRecords } from "../display-data-readers";
import { DETAIL_ROW_CLASS, listDetail } from "./detail-parts";

/** 结果图片按对话图片的统一外观横排；地址按消息附件同一规则解析。 */
function MCPResultImages({ assetIds }: { assetIds: string[] }) {
  const workspaceId = useAttachmentWorkspaceId();
  const images = useMemo(
    () => assetIds.map((assetId) => ({ src: attachmentContentUrl(assetId, workspaceId), alt: "MCP 工具返回的图片" })),
    [assetIds, workspaceId],
  );
  return <ImageStrip images={images} label="MCP 工具返回的图片" />;
}

/**
 * `mcp-tool-result`：一次 MCP 工具调用。结果正文只进模型上下文，服务与工具已在目标里（`服务/工具`），元信息只补
 * 「工作区」（服务定义在工作区自己的配置里）与图片张数；结果带图片（`images` 为附件 asset id）时展开可看缩略图。
 */
export const mcpView: CallView = (data) => {
  const images = readArray(data.images).filter((item): item is string => typeof item === "string");
  return {
    meta: [data.scope === "workspace" ? "工作区" : null, images.length > 0 ? `${images.length} 张图片` : null].filter(Boolean).join(" · ") || undefined,
    detail: images.length > 0 ? { render: () => <MCPResultImages assetIds={images} /> } : undefined,
  };
};

/** `skill-content`：`list` 列出可用 Skill，工作区自己的 Skill 在列表与调用行都标「工作区」；`load` 的正文只进模型上下文，Skill 名已在目标里，元信息只补「工作区」。 */
export const skillView: CallView = (data) => {
  return {
    meta: data.action === "list"
      ? `${readArray(data.skills).length} 个 Skill`
      : data.scope === "workspace" ? "工作区" : undefined,
    detail: data.action === "list" ? listDetail(data, () => readRecords(data.skills), "没有可用 Skill", (skill, index) => (
      <li key={`${String(skill.name ?? "skill")}-${index}`} className={DETAIL_ROW_CLASS}>
        <span className="shrink-0 font-medium">{String(skill.name ?? "未命名 Skill")}</span>
        {skill.scope === "workspace" && <span className="text-muted-foreground shrink-0">工作区</span>}
        {typeof skill.description === "string" && skill.description && <span className="text-muted-foreground truncate">{skill.description}</span>}
      </li>
    )) : undefined,
  };
};
