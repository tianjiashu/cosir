import { useOpenLink } from "@/components/links/use-open-link";
import { useOpenWorkspacePath } from "@/components/workspace-references/use-open-workspace-path";
import { CallOpenButton } from "./call-open-button";

/**
 * 调用行尾的「打开」：在文件面板打开这次调用涉及的工作区文件（读取、改动），`line` 给出时定位到该行。
 *
 * 放在行外的 `actions` 而不是让路径本身可点：行有详情时整行是展开按钮，里面再放按钮会嵌套、一次点击同时
 * 打开文件又展开详情。路径不在当前工作区或没有活动工作区时不渲染，不给点了没反应的入口。
 */
export function OpenWorkspaceFileButton({ path, line }: { path: string; line?: number; }) {
  const files = useOpenWorkspacePath();
  const links = useOpenLink("tool");
  const target = files?.resolve(path) ?? null;
  if (target === null) return null;
  return (
    <CallOpenButton
      label={`在文件面板打开 ${target}`}
      onClick={() => links.open({ kind: "workspace", path: target, directory: false, lines: line ? { start: line, end: line } : undefined })}
    />
  );
}
