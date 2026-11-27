import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { updateModelConfig, type ModelConfig, type ModelConnection } from "@/lib/api/model-connections";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useFieldEdits } from "@/lib/form/use-field-edits";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";
import { useUnmountSignal } from "@/hooks/use-unmount-signal";
import { capabilitiesValid, ModelCapabilityFields, pickCapabilities } from "./model-capability-fields";
import { testModel } from "./test-model-drafts";

/**
 * 在模型行里展开，改上下文窗口与能力。能力会改变请求参数，所以保存前按新能力测一次；
 * 模型名不可改（改名会让引用它的对话与 Agent 悄悄换模型），换模型请新增后删除旧的。
 *
 * 只提交改过的能力（`useFieldEdits`）：外部改过的其他能力在列表重新读取后保留，不被这次保存改回去。
 * 测试途中编辑器被关掉时探活随之中止、不再写入。
 */
export function ModelCapabilitiesEditor({ connection, model, onClose }: { connection: ModelConnection; model: ModelConfig; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { values: capabilities, changes, dirty, edit } = useFieldEdits(pickCapabilities(model));
  const lifetime = useUnmountSignal();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useUnsavedChanges(`模型「${model.model_name}」`, dirty);

  const save = async () => {
    if (!dirty || !capabilitiesValid(capabilities) || busy) return;
    setBusy(true);
    setError(null);
    const signal = lifetime();
    const test = await testModel({ base_url: connection.base_url, api_key: connection.api_key }, { model_name: model.model_name, ...capabilities }, signal);
    if (signal.aborted) return;
    if (test.kind === "failed") {
      setError(`测试未通过：${test.message}`);
      setBusy(false);
      return;
    }
    try {
      await updateModelConfig(connection.connection_id, model.model_name, changes);
    } catch (cause) {
      setError(userErrorMessage(cause, "保存失败，请重试"));
      void frontendLog("ERROR", "model_config_update_failed", "保存模型失败", { data: { configId: model.config_id }, error: cause });
      setBusy(false);
      return;
    }
    void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections() });
    notify.success("模型已保存");
    onClose();
  };

  return (
    <div className="grid gap-2">
      <ModelCapabilityFields value={capabilities} onChange={edit} disabled={busy} label={model.model_name} />
      {error && <p className="text-destructive text-xs">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="xs" disabled={busy} onClick={() => void confirmDiscardUnsaved().then((discard) => { if (discard) onClose(); })}>取消</Button>
        <Button size="xs" disabled={busy || !dirty || !capabilitiesValid(capabilities)} onClick={() => void save()}>
          {busy && <Loader2Icon className="animate-spin" />}测试并保存
        </Button>
      </div>
    </div>
  );
}
