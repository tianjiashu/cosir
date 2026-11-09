import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { updateModelConnection, type ModelConnection, type ModelConnectionFields } from "@/lib/api/model-connections";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";
import { ConnectionFields, connectionFieldsComplete } from "./connection-fields";
import { testModelDrafts } from "./test-model-drafts";

/**
 * 编辑连接的名称、地址与 Key（在连接卡片里展开）。
 *
 * 只改名称直接保存；改了地址或 Key 时，先用该连接下第一个模型按新凭据测一次——验证的是 Key 与地址，
 * 不逐个复测全部模型（几十个模型逐个探活代价过高，模型本身没变）。连接下还没有模型时无从测试，直接保存。
 */
export function ConnectionSettingsEditor({ connection, onClose }: { connection: ModelConnection; onClose: () => void }) {
  const queryClient = useQueryClient();
  const original: ModelConnectionFields = { name: connection.name, base_url: connection.base_url, api_key: connection.api_key };
  const [fields, setFields] = useState(original);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const changes = Object.fromEntries(
    (Object.keys(original) as (keyof ModelConnectionFields)[])
      .filter((key) => fields[key].trim() !== original[key])
      .map((key) => [key, fields[key].trim()]),
  ) as Partial<ModelConnectionFields>;
  const dirty = Object.keys(changes).length > 0;
  const probe = changes.base_url !== undefined || changes.api_key !== undefined ? connection.models[0] : undefined;
  useUnsavedChanges(`连接「${connection.name}」`, dirty);

  const save = async () => {
    if (!dirty || !connectionFieldsComplete(fields) || busy) return;
    setBusy(true);
    setError(null);
    if (probe) {
      let failure = "";
      const passed = await testModelDrafts(
        { base_url: fields.base_url.trim(), api_key: fields.api_key.trim() },
        [probe],
        (_name, test) => { if (test.kind === "failed") failure = test.message; },
      );
      if (passed.length === 0) {
        setError(`用「${probe.model_name}」测试未通过：${failure}`);
        setBusy(false);
        return;
      }
    }
    try {
      await updateModelConnection(connection.connection_id, changes);
    } catch (cause) {
      setError(userErrorMessage(cause, "保存失败，请重试"));
      void frontendLog("ERROR", "model_connection_update_failed", "保存模型连接失败", {
        data: { connectionId: connection.connection_id, fields: Object.keys(changes) },
        error: cause,
      });
      setBusy(false);
      return;
    }
    void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections() });
    notify.success("连接已保存");
    onClose();
  };

  return (
    <div className="grid gap-3">
      <ConnectionFields value={fields} onChange={setFields} disabled={busy} />
      {error && <Alert tone="destructive">{error}</Alert>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {probe && <span className="text-muted-foreground mr-auto text-xs">改了地址或 Key，保存前会用「{probe.model_name}」测试一次</span>}
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void confirmDiscardUnsaved().then((discard) => { if (discard) onClose(); })}>取消</Button>
        <Button size="sm" disabled={busy || !dirty || !connectionFieldsComplete(fields)} onClick={() => void save()}>
          {busy && <Loader2Icon className="animate-spin" />}
          {probe ? "测试并保存" : "保存"}
        </Button>
      </div>
    </div>
  );
}
