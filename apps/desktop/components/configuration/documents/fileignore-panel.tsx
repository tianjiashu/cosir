import { Loader2Icon, SaveIcon } from "lucide-react";

import { useConfigurationDocument } from "@/components/configuration/documents/use-configuration-document";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Textarea } from "@/components/ui/textarea";
import { getWorkspaceFileIgnoreConfiguration, updateWorkspaceFileIgnoreConfiguration } from "@/lib/api/configuration";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useAppHotkey, withShortcut } from "@/lib/keyboard/shortcuts";
import { queryKeys } from "@/lib/query/query-keys";

/** 工作区配置的「.fileignore」标签：纯文本编辑，规则语法与上限由后端校验。 */
export function FileIgnorePanel({ workspaceId }: { workspaceId: number }) {
  const state = useConfigurationDocument({
    queryKey: queryKeys.workspaceFileIgnore(workspaceId),
    label: ".fileignore",
    load: (options) => getWorkspaceFileIgnoreConfiguration(workspaceId, options),
    save: (content, baseRevision) => updateWorkspaceFileIgnoreConfiguration(workspaceId, content, baseRevision),
    saveFailedEvent: "workspace_fileignore_save_failed",
  });
  useAppHotkey("saveConfiguration", state.save);

  if (!state.document) {
    return state.loadError
      ? <ErrorState className="my-10" message={userErrorMessage(state.loadError, "读取 .fileignore 失败")} onRetry={state.reload} retryLabel="重新读取" />
      : <LoadingState label="正在读取 .fileignore…" />;
  }
  return (
    <div className="space-y-4">
      {state.saveError !== null && <Alert tone="destructive">{userErrorMessage(state.saveError, "保存 .fileignore 失败")}</Alert>}
      <Card className="p-4">
        <p className="text-muted-foreground mb-3 text-xs">控制工作区搜索时跳过哪些文件和目录。支持 gitignore 语法。</p>
        <Textarea
          className="min-h-[28rem] resize-y border-0 bg-transparent p-2 font-mono text-sm shadow-none focus-visible:ring-0"
          value={state.content}
          onChange={(event) => state.setContent(event.target.value)}
        />
        <div className="mt-4 flex items-center justify-end gap-2 border-t pt-4">
          {state.dirty && <span className="text-muted-foreground mr-auto text-xs">有未保存更改</span>}
          <Tooltip>
            <TooltipTrigger render={<Button onClick={state.save} disabled={state.saving} />}>
              {state.saving ? <><Loader2Icon className="animate-spin" />保存中…</> : <><SaveIcon />保存 .fileignore</>}
            </TooltipTrigger>
            <TooltipContent>{withShortcut("保存 .fileignore", "saveConfiguration")}</TooltipContent>
          </Tooltip>
        </div>
      </Card>
    </div>
  );
}
