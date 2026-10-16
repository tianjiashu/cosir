"use client";

import { useState } from "react";
import { useStore } from "zustand";
import type { TeamEditorSession, PersistTeam } from "@/lib/agent-team-editor/session";
import { AgentTeamConfigurationForm } from "@/components/agent-team-configuration-form";

import { Button } from "@/components/ui/button";
import { Card, CardAction, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { AgentTeamGraphEditor } from "@/components/agent-team-graph-editor";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

type AgentProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

const EDITOR_MODE_OPTIONS = [
  { value: "form", label: "表单" },
  { value: "canvas", label: "画布" },
] as const;

/** 表单和画布只投影同一会话；保存及历史生命周期不依赖视图挂载或持久化键。 */
export function AgentTeamConfigurationEditor({ session, profilesByScope, scopeEditable, onCancel, onSave }: {
  session: TeamEditorSession;
  profilesByScope: AgentProfilesByScope;
  scopeEditable: boolean;
  onCancel: () => void;
  onSave: PersistTeam;
}) {
  const { snapshot: { configuration }, baseline, saveError, saving, mode: editorMode, history, dirty } = useStore(session.store);
  const [canvasVisited, setCanvasVisited] = useState(editorMode === "canvas");
  const profiles = profilesByScope[configuration.scope];
  const save = async () => {
    if (await session.save(onSave)) notify.success(session.store.getState().dirty ? "已保存提交版本，仍有新修改待保存" : "Agent Team 配置已保存");
  };

  return (
    <Card className={cn(editorMode === "canvas" && "min-w-0 rounded-none border-0 bg-transparent shadow-none")}>
      <CardHeader>
        <CardTitle>{baseline ? "编辑 Agent Team" : "新建 Agent Team"}</CardTitle>
        <CardAction className="flex flex-wrap items-center justify-end gap-2">
          <span role="status" className="text-xs text-muted-foreground">{saving ? "保存中…" : dirty ? "未保存" : baseline ? "已保存" : "新建草稿"}</span>
          {editorMode === "form" && <><Button type="button" variant="outline" onClick={session.undo} disabled={!history.past.length}>撤销</Button><Button type="button" variant="outline" onClick={session.redo} disabled={!history.future.length}>重做</Button></> }
          <SegmentedControl
            aria-label="编辑模式"
            className="w-40"
            value={editorMode}
            options={EDITOR_MODE_OPTIONS}
            onValueChange={(mode) => {
              // 画布首次进入时才挂载，之后保持挂载以保留画布测量与输入焦点。
              if (mode === "canvas") setCanvasVisited(true);
              session.setMode(mode);
            }}
          />
          <Button type="button" variant="outline" onClick={onCancel}>返回 Team 列表</Button>
        </CardAction>
      </CardHeader>
      <div className={editorMode === "form" ? "block" : "hidden"}>
        <AgentTeamConfigurationForm
          session={session}
          profiles={profiles}
          scopeEditable={scopeEditable && !baseline && !saving}
          teamIdEditable={!baseline && !saving}
          saving={saving}
          onSave={save}
          onCancel={onCancel}
        />
      </div>
      {canvasVisited && <div className={editorMode === "canvas" ? "min-w-0" : "hidden"}>
        <AgentTeamGraphEditor
          session={session}
          profiles={profiles}
          onSave={save}
          onExit={() => session.setMode("form")}
          saving={saving}
          scopeEditable={scopeEditable && !baseline && !saving}
          teamIdEditable={!baseline && !saving}
        />
      </div>}
      <Dialog open={saveError !== null} onOpenChange={(open) => { if (!open) session.clearSaveError(); }}>
        <DialogContent showCloseButton={false}>
          <DialogTitle>保存失败</DialogTitle>
          <DialogDescription className="whitespace-pre-wrap text-destructive">
            {saveError}
          </DialogDescription>
          <DialogFooter>
            <Button type="button" onClick={() => session.clearSaveError()}>返回修改</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
