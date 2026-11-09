import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { ModelConfig, ModelConnection } from "@/lib/api/model-connections";
import { cn } from "@/lib/utils";
import { ConnectionSettingsEditor } from "./connection-settings-editor";
import { capabilitySummary } from "./model-capability-fields";
import { ModelConfigEditor } from "./model-config-editor";
import { ModelsEditor } from "./models-editor";

/** 卡片里当前展开的编辑器；整个模型面板同一时刻只有一个，由面板持有。 */
export type ConnectionEditor =
  | { kind: "create" }
  | { kind: "edit-connection"; connectionId: string }
  | { kind: "add-models"; connectionId: string }
  | { kind: "edit-model"; configId: string };

function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl;
  }
}

/**
 * 一个连接及其下模型：标题行是连接（名称、主机、模型数与操作），下面每行一个模型（能力摘要，悬停显示操作）。
 * 编辑连接、添加模型、编辑模型都在卡片里就地展开，不弹对话框。
 */
export function ModelConnectionCard({
  connection,
  editor,
  onEdit,
  onClose,
  onDeleteConnection,
  onDeleteModel,
}: {
  connection: ModelConnection;
  editor: ConnectionEditor | null;
  onEdit: (editor: ConnectionEditor) => void;
  onClose: () => void;
  onDeleteConnection: (connection: ModelConnection) => void;
  onDeleteModel: (model: ModelConfig) => void;
}) {
  const id = connection.connection_id;
  const editingConnection = editor?.kind === "edit-connection" && editor.connectionId === id;
  const addingModels = editor?.kind === "add-models" && editor.connectionId === id;

  return (
    <section aria-label={`连接 ${connection.name}`} className="border-border/70 overflow-hidden rounded-lg border">
      <div className="bg-muted/30 border-border/70 border-b px-3 py-2.5">
        {editingConnection ? (
          <ConnectionSettingsEditor connection={connection} onClose={onClose} />
        ) : (
          <div className="flex min-w-0 items-center gap-2">
            <div className="flex min-w-0 flex-1 items-baseline gap-2">
              <h3 className="truncate text-sm font-medium">{connection.name}</h3>
              <span className="text-muted-foreground truncate text-xs" title={connection.base_url}>{hostOf(connection.base_url)}</span>
              <span className="text-muted-foreground shrink-0 text-xs">{connection.models.length} 个模型</span>
            </div>
            <Button variant="ghost" size="sm" disabled={addingModels} onClick={() => onEdit({ kind: "add-models", connectionId: id })}>
              <PlusIcon />添加模型
            </Button>
            <TooltipIconButton variant="ghost" size="icon-sm" tooltip="编辑连接" aria-label={`编辑连接 ${connection.name}`} onClick={() => onEdit({ kind: "edit-connection", connectionId: id })}>
              <PencilIcon />
            </TooltipIconButton>
            <TooltipIconButton variant="ghost" size="icon-sm" tooltip="删除连接" aria-label={`删除连接 ${connection.name}`} onClick={() => onDeleteConnection(connection)}>
              <Trash2Icon />
            </TooltipIconButton>
          </div>
        )}
      </div>
      {connection.models.length === 0 && !addingModels && (
        <p className="text-muted-foreground px-3 py-3 text-xs">这个连接下还没有模型，点「添加模型」选择要用的模型。</p>
      )}
      <ul className="divide-border/70 divide-y">
        {connection.models.map((model) => {
          const editing = editor?.kind === "edit-model" && editor.configId === model.config_id;
          return (
            <li key={model.config_id} className={cn("group/model px-3 py-2", editing && "bg-muted/30")}>
              <div className="flex min-h-7 min-w-0 items-center gap-3">
                <span className="min-w-0 truncate text-sm" title={model.model_name}>{model.model_name}</span>
                <span className="text-muted-foreground min-w-0 flex-1 truncate text-xs">{capabilitySummary(model)}</span>
                {!editing && (
                  <div className="flex shrink-0 items-center gap-0.5 opacity-0 group-hover/model:opacity-100 group-focus-within/model:opacity-100 [@media(hover:none)]:opacity-100">
                    <TooltipIconButton variant="ghost" size="icon-xs" tooltip="编辑模型" aria-label={`编辑模型 ${model.model_name}`} onClick={() => onEdit({ kind: "edit-model", configId: model.config_id })}>
                      <PencilIcon />
                    </TooltipIconButton>
                    <TooltipIconButton variant="ghost" size="icon-xs" tooltip="删除模型" aria-label={`删除模型 ${model.model_name}`} onClick={() => onDeleteModel(model)}>
                      <Trash2Icon />
                    </TooltipIconButton>
                  </div>
                )}
              </div>
              {editing && <div className="pt-2"><ModelConfigEditor connection={connection} model={model} onClose={onClose} /></div>}
            </li>
          );
        })}
      </ul>
      {addingModels && (
        <div className="border-border/70 border-t p-3">
          <ModelsEditor connection={connection} onClose={onClose} />
        </div>
      )}
    </section>
  );
}
