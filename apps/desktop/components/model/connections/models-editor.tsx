import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  addModelConfigs,
  createModelConnection,
  type ModelCapabilities,
  type ModelConfigFields,
  type ModelConnection,
  type ModelConnectionFields,
} from "@/lib/api/model-connections";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";
import { ConnectionFields, connectionFieldsComplete } from "./connection-fields";
import { capabilitiesValid, DEFAULT_CAPABILITIES, pickCapabilities } from "./model-capability-fields";
import { ModelDraftList, type ModelDraft } from "./model-draft-list";
import { ModelPicker } from "./model-picker";
import { testModelDrafts } from "./test-model-drafts";
import { useModelDiscovery } from "./use-model-discovery";

const EMPTY_CONNECTION: ModelConnectionFields = { name: "", base_url: "", api_key: "" };

function fieldsOf(draft: ModelDraft): ModelConfigFields {
  return { model_name: draft.model_name, ...pickCapabilities(draft) };
}

/**
 * 一次添加多个模型的编辑卡：新建连接（`connection` 不传）或给已有连接追加模型。
 *
 * 只有一个提交动作「测试并保存」：逐个探活（进度逐行显示），通过的一次写入，失败的留在卡里写明原因，
 * 改完可再提交——「测试通过才能保存」按模型把关，单个失败不拖累其余。新建连接时首次保存即建出连接，
 * 之后连接字段锁定，再提交的模型追加到这个连接下。全部保存后卡片关闭。
 */
export function ModelsEditor({ connection, onClose }: { connection?: ModelConnection; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [fields, setFields] = useState<ModelConnectionFields>(EMPTY_CONNECTION);
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [saved, setSaved] = useState<ReadonlySet<string>>(new Set());
  const [drafts, setDrafts] = useState<ModelDraft[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const creating = connection === undefined;
  const target = connection ?? fields;
  const discovery = useModelDiscovery(target.base_url, target.api_key);
  const existing = new Set([...(connection?.models.map((model) => model.model_name) ?? []), ...saved]);
  const selected = new Set(drafts.map((draft) => draft.model_name));
  const fieldsTouched = creating && createdId === null && Object.values(fields).some((value) => value.trim());
  useUnsavedChanges(creating ? "新建模型连接" : "添加模型", drafts.length > 0 || fieldsTouched);

  const blocker = creating && !connectionFieldsComplete(fields)
    ? "填写连接名称、API Base URL 和 API Key"
    : drafts.length === 0
      ? "至少选择一个模型"
      : drafts.some((draft) => !capabilitiesValid(draft))
        ? "上下文窗口需为正整数"
        : null;

  const pick = (next: Set<string>) => {
    setDrafts((current) => [
      ...current.filter((draft) => next.has(draft.model_name)),
      ...[...next]
        .filter((name) => !current.some((draft) => draft.model_name === name))
        .map((name): ModelDraft => ({ model_name: name, ...DEFAULT_CAPABILITIES, test: { kind: "idle" } })),
    ]);
  };
  const edit = (modelName: string, capabilities: ModelCapabilities) => {
    setDrafts((current) => current.map((draft) => (
      draft.model_name === modelName ? { ...draft, ...capabilities, test: { kind: "idle" } } : draft
    )));
  };

  const save = async (models: ModelConfigFields[]) => {
    const connectionId = connection?.connection_id ?? createdId;
    if (connectionId !== null) {
      await addModelConfigs(connectionId, models);
      return;
    }
    const created = await createModelConnection({
      name: fields.name.trim(),
      base_url: fields.base_url.trim(),
      api_key: fields.api_key.trim(),
      models,
    });
    setCreatedId(created.connection_id);
  };

  const submit = async () => {
    if (blocker || busy) return;
    setBusy(true);
    setError(null);
    const credentials = { base_url: target.base_url.trim(), api_key: target.api_key.trim() };
    const passed = await testModelDrafts(credentials, drafts.map(fieldsOf), (name, test) => {
      setDrafts((current) => current.map((draft) => (draft.model_name === name ? { ...draft, test } : draft)));
    });
    if (passed.length === 0) {
      setError("没有模型通过测试，请检查连接信息或模型设置后重试");
      setBusy(false);
      return;
    }
    try {
      await save(passed);
    } catch (cause) {
      setError(userErrorMessage(cause, "保存失败，请重试"));
      void frontendLog("ERROR", "model_configs_save_failed", "保存模型失败", {
        data: { connectionId: connection?.connection_id ?? createdId, models: passed.map((model) => model.model_name) },
        error: cause,
      });
      setBusy(false);
      return;
    }
    void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections() });
    const passedNames = new Set(passed.map((model) => model.model_name));
    const remaining = drafts.length - passed.length;
    setDrafts((current) => current.filter((draft) => !passedNames.has(draft.model_name)));
    setSaved((current) => new Set([...current, ...passedNames]));
    setBusy(false);
    if (remaining === 0) {
      notify.success(`已添加 ${passed.length} 个模型`);
      onClose();
    } else {
      notify.success(`已添加 ${passed.length} 个模型`, { description: `${remaining} 个未通过测试，已保留在编辑卡里` });
    }
  };

  const cancel = async () => {
    if (await confirmDiscardUnsaved()) onClose();
  };

  return (
    <section
      aria-label={creating ? "添加连接" : `给「${connection.name}」添加模型`}
      className="border-border bg-card grid min-w-0 gap-4 rounded-lg border p-4"
    >
      <h3 className="text-sm font-medium">{creating ? "添加连接" : `给「${connection.name}」添加模型`}</h3>
      {creating && (
        <div className="grid gap-1.5">
          <ConnectionFields value={fields} onChange={setFields} disabled={busy || createdId !== null} />
          {createdId !== null && <p className="text-muted-foreground text-xs">连接已创建；下面是还没保存的模型，改好后可再次提交。</p>}
        </div>
      )}
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <ModelPicker discovery={discovery} selected={selected} existing={existing} onChange={pick} disabled={busy} />
        <ModelDraftList
          drafts={drafts}
          onChange={edit}
          onRemove={(name) => setDrafts((current) => current.filter((draft) => draft.model_name !== name))}
          disabled={busy}
        />
      </div>
      {error && <Alert tone="destructive">{error}</Alert>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {blocker && <span className="text-muted-foreground mr-auto text-xs">{blocker}</span>}
        <Button variant="ghost" disabled={busy} onClick={() => void cancel()}>取消</Button>
        <Button disabled={busy || blocker !== null} onClick={() => void submit()}>
          {busy && <Loader2Icon className="animate-spin" />}
          {drafts.length > 0 ? `测试并保存 ${drafts.length} 个模型` : "测试并保存"}
        </Button>
      </div>
    </section>
  );
}
