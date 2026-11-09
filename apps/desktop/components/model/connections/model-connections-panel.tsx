import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon, ServerIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { deleteModelConfig, deleteModelConnection, type ModelConfig, type ModelConnection } from "@/lib/api/model-connections";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { modelConnectionsQueryOptions } from "@/lib/query/query-options";
import { confirmDiscardUnsaved } from "@/lib/unsaved-changes";
import { ModelConnectionCard, type ConnectionEditor } from "./model-connection-card";
import { ModelsEditor } from "./models-editor";

/**
 * 配置中心「模型」页：按连接分组的模型列表，以及唯一一个展开中的编辑器（新建连接、编辑连接、
 * 添加模型或编辑模型）。切换编辑器前先确认放弃当前未保存的修改；离开页面由路由守卫确认。
 *
 * 事实源是系统级模型配置文件（顶部展示路径，可直接手改；切回窗口时重新读取）。列表与模型选择目录
 * 共用 `queryKeys.modelConnections()` 一份缓存：各编辑器写入后失效它，选择器随之刷新。
 * 文件写坏时整页显示后端指出的出错位置，写入一律被后端拒绝。
 */
export function ModelConnectionsPanel() {
  const queryClient = useQueryClient();
  const connectionsQuery = useQuery(modelConnectionsQueryOptions());
  const [editor, setEditor] = useState<ConnectionEditor | null>(null);

  const open = async (next: ConnectionEditor) => {
    if (editor && !(await confirmDiscardUnsaved())) return;
    setEditor(next);
  };
  const close = () => setEditor(null);
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections() });

  const deleteConnection = useMutation({ mutationFn: deleteModelConnection, onSuccess: invalidate });
  const deleteModel = useMutation({
    mutationFn: (model: ModelConfig) => deleteModelConfig(model.connection_id, model.model_name),
    onSuccess: invalidate,
  });

  const removeConnection = async (connection: ModelConnection) => {
    const removed = await confirmDelete({
      target: `连接「${connection.name}」`,
      consequence: connection.models.length > 0
        ? `将同时删除它下面的 ${connection.models.length} 个模型，使用这些模型的对话将无法继续选择它们。`
        : "删除后需要重新填写地址和 API Key。",
      action: () => deleteConnection.mutateAsync(connection.connection_id),
    });
    if (!removed) return;
    if (editor && editor.kind !== "create") close();
    notify.success("连接已删除");
  };
  const removeModel = async (model: ModelConfig) => {
    const removed = await confirmDelete({
      target: `模型「${model.model_name}」`,
      consequence: "使用该模型的对话将无法继续选择它。",
      action: () => deleteModel.mutateAsync(model),
    });
    if (!removed) return;
    if (editor?.kind === "edit-model" && editor.configId === model.config_id) close();
    notify.success("模型已删除");
  };

  const connections = connectionsQuery.data?.connections;
  return (
    <div className="grid min-w-0 gap-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-medium">模型</h2>
          <p className="text-muted-foreground mt-1 text-xs">连接保存 OpenAI 兼容接口的地址和 API Key，一个连接下可以挂多个模型；模型测试通过后才会保存。</p>
          {connectionsQuery.data && (
            <p className="text-muted-foreground mt-1 flex min-w-0 items-center gap-1 text-xs">
              <span className="shrink-0">配置文件</span>
              <code className="bg-muted min-w-0 truncate rounded px-1 font-mono" title={connectionsQuery.data.path}>{connectionsQuery.data.path}</code>
              <CopyIconButton
                variant="ghost"
                size="icon-xs"
                label="复制配置文件路径"
                getText={() => connectionsQuery.data?.path ?? ""}
                logEvent="model_config_path_copy_failed"
              />
              <span className="shrink-0">可直接编辑，切回应用后自动刷新。</span>
            </p>
          )}
        </div>
        {connections && connections.length > 0 && (
          <Button size="sm" className="shrink-0" disabled={editor?.kind === "create"} onClick={() => void open({ kind: "create" })}>
            <PlusIcon />添加连接
          </Button>
        )}
      </div>
      {editor?.kind === "create" && <ModelsEditor onClose={close} />}
      {connectionsQuery.isPending ? (
        <LoadingState label="正在读取模型配置…" size="compact" />
      ) : connectionsQuery.isError ? (
        <ErrorState message={userErrorMessage(connectionsQuery.error, "模型配置加载失败，请重试")} onRetry={() => void connectionsQuery.refetch()} />
      ) : connectionsQuery.data.connections.length === 0 && editor?.kind !== "create" ? (
        <EmptyState
          icon={<ServerIcon className="size-5" />}
          title="还没有模型连接"
          description="添加一个 OpenAI 兼容接口，一次选好要用的模型。"
          action={<Button size="sm" onClick={() => void open({ kind: "create" })}><PlusIcon />添加连接</Button>}
        />
      ) : (
        connectionsQuery.data.connections.map((connection) => (
          <ModelConnectionCard
            key={connection.connection_id}
            connection={connection}
            editor={editor}
            onEdit={(next) => void open(next)}
            onClose={close}
            onDeleteConnection={(target) => void removeConnection(target)}
            onDeleteModel={(target) => void removeModel(target)}
          />
        ))
      )}
    </div>
  );
}
