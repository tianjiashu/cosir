import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SaveIcon } from "lucide-react";

import { EnvironmentForm } from "@/components/configuration/environment/environment-form";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import {
  getEnvironmentConfiguration,
  type EnvironmentField,
  type EnvironmentGroup,
  updateEnvironmentConfiguration,
} from "@/lib/api/configuration";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges } from "@/lib/unsaved-changes";

type EnvironmentChanges = Parameters<typeof updateEnvironmentConfiguration>[0];

/** 服务端字段值到表单值：密钥永不回显（留空表示不变）。 */
function environmentFormValues(groups: EnvironmentGroup[]): Record<string, string | boolean> {
  return Object.fromEntries(groups.flatMap((group) => group.fields).map((field) => [
    field.name,
    field.secret ? "" : field.type === "boolean" ? Boolean(field.value) : String(field.value ?? ""),
  ]));
}

function environmentChange(field: EnvironmentField, value: string | boolean, clearSecret: boolean): EnvironmentChanges[string] {
  const operation = field.secret
    ? clearSecret && field.clearable ? "clear" : value === "" ? "unchanged" : "replace"
    : field.type === "boolean" ? "replace" : value === "" && field.clearable ? "clear" : "replace";
  return { operation, value };
}

/** 系统配置的「环境变量」标签：读取白名单字段，交给编辑副本。 */
export function EnvironmentPanel() {
  const query = useQuery({
    queryKey: queryKeys.environment(),
    queryFn: ({ signal }) => getEnvironmentConfiguration({ signal }),
  });
  if (!query.data) {
    return query.isError
      ? <ErrorState className="my-10" message={userErrorMessage(query.error, "读取环境变量失败")} onRetry={() => void query.refetch()} retryLabel="重新读取" />
      : <LoadingState label="正在读取系统配置…" />;
  }
  return <EnvironmentEditor groups={query.data.groups} />;
}

/**
 * 环境变量编辑副本：只记录用户改动过的字段（edits），其余字段跟随服务端，
 * 因此后台重新读取不会覆盖未保存的改动；保存时只提交改动字段。
 */
function EnvironmentEditor({ groups }: { groups: EnvironmentGroup[] }) {
  const queryClient = useQueryClient();
  const [edits, setEdits] = useState<Record<string, string | boolean>>({});
  const [clearSecrets, setClearSecrets] = useState<Set<string>>(() => new Set());
  const serverValues = useMemo(() => environmentFormValues(groups), [groups]);
  const values = { ...serverValues, ...edits };
  const dirtyCount = Object.keys(edits).length;
  useUnsavedChanges("环境变量", dirtyCount > 0);

  const mutation = useMutation({
    mutationFn: updateEnvironmentConfiguration,
    onSuccess: (result) => {
      queryClient.setQueryData(queryKeys.environment(), result);
      setEdits({});
      setClearSecrets(new Set());
      notify.success("环境变量已保存");
    },
    onError: (error, changes) => {
      void frontendLog("ERROR", "environment_configuration_save_failed", "保存环境变量失败", {
        data: { fields: Object.keys(changes) },
        error,
      });
    },
  });

  const save = () => {
    const fields = new Map(groups.flatMap((group) => group.fields).map((field) => [field.name, field]));
    const changes: EnvironmentChanges = Object.fromEntries(Object.entries(edits).map(([name, value]) => {
      const field = fields.get(name);
      // 后端换代后字段可能已下线：不提交未知字段的值。
      return [name, field ? environmentChange(field, value, clearSecrets.has(name)) : { operation: "unchanged" }];
    }));
    mutation.mutate(changes);
  };

  const edit = (field: EnvironmentField, value: string | boolean) => {
    setEdits((current) => ({ ...current, [field.name]: value }));
    if (mutation.isError) mutation.reset();
  };

  const forgetClear = (name: string) => setClearSecrets((current) => {
    const next = new Set(current);
    next.delete(name);
    return next;
  });

  return (
    <div className="space-y-4">
      <div className="bg-background/95 border-border/70 sticky top-0 z-10 -mx-2 flex flex-wrap items-center justify-between gap-3 border-b px-2 py-3 backdrop-blur-sm">
        <div>
          <h2 className="font-medium">环境变量</h2>
          <p className="text-muted-foreground mt-1 text-xs">管理本机 Agent 的运行环境配置。</p>
        </div>
        <Button onClick={save} disabled={mutation.isPending || dirtyCount === 0}>
          {mutation.isPending ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存变量{dirtyCount > 0 ? `（${dirtyCount}）` : ""}
        </Button>
      </div>
      {mutation.isError && <Alert tone="destructive">{userErrorMessage(mutation.error, "保存环境变量失败")}</Alert>}
      {/* 保存期间锁定表单，避免请求进行中的改动在成功后被一并清空。 */}
      <fieldset disabled={mutation.isPending} className="m-0 min-w-0 border-0 p-0">
        <EnvironmentForm
          groups={groups}
          values={values}
          onChange={(field, value) => {
            edit(field, value);
            forgetClear(field.name);
          }}
          onToggle={(field) => edit(field, values[field.name] !== true)}
          onClear={(field) => {
            edit(field, "");
            setClearSecrets((current) => new Set(current).add(field.name));
          }}
          pendingClears={clearSecrets}
          onUndoClear={(field) => {
            // 撤销即回到「未改动」：从改动集合中移除，保存时不再提交该字段。
            setEdits((current) => {
              const next = { ...current };
              delete next[field.name];
              return next;
            });
            forgetClear(field.name);
          }}
        />
      </fieldset>
    </div>
  );
}
