import { useState } from "react";
import { CheckCircle2Icon, Loader2Icon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import type { ClaudeModelChoice } from "@/lib/api/claude-subscription";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { notify } from "@/lib/notify";
import type { ClaudeSubscriptionConnectionState } from "./use-claude-subscription";

export function ClaudeSubscriptionConnection({ connection, disabled = false }: {
  connection: ClaudeSubscriptionConnectionState;
  disabled?: boolean;
}) {
  const [code, setCode] = useState("");
  const { statusQuery, login, busy, error } = connection;
  const status = statusQuery.data;
  const locked = disabled || busy;
  const statusLabel = status?.status === "connected" ? "已登录"
    : status?.status === "expired" ? "登录已过期" : "未登录";

  async function copyLink() {
    if (!login) return;
    try {
      await navigator.clipboard.writeText(login.authorization_url);
      notify.success("授权链接已复制");
    } catch {
      notify.error("复制失败，请选中授权链接手动复制");
    }
  }

  return (
    <div className="bg-muted/30 space-y-3 rounded-md border p-3" aria-label="Claude 订阅账号">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="space-y-1 text-xs">
          <div className="flex items-center gap-1.5 font-medium">
            {connection.connected && <CheckCircle2Icon className="text-success size-4" />}
            Claude 账号
            <span className="text-muted-foreground font-normal">{statusQuery.isPending ? "正在读取登录状态…" : statusLabel}</span>
          </div>
          {status?.account_label && <p>{status.account_label}</p>}
          <p className="text-muted-foreground">凭据由 Cosir 保存在本机，连接 Claude 官方服务。</p>
          {status?.status === "connected" && <p className="text-muted-foreground">
            {status.can_refresh ? "登录到期时自动刷新。" : "已导入本机登录态；到期后请通过浏览器重新登录。"}
            {status.expires_at && ` 当前凭据有效至 ${new Date(status.expires_at * 1000).toLocaleString()}`}
          </p>}
        </div>
        {busy && <Loader2Icon className="text-muted-foreground size-4 animate-spin" aria-label="正在处理账号操作" />}
      </div>
      {statusQuery.isError && <div className="space-y-2">
        <Alert tone="destructive">{userErrorMessage(statusQuery.error, "登录状态读取失败")}</Alert>
        <Button size="sm" variant="outline" onClick={() => void statusQuery.refetch()}>重试登录状态</Button>
      </div>}
      {!login ? <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={locked} onClick={() => void connection.start()}>
          {status?.status === "connected" ? "重新登录" : "登录 Claude"}
        </Button>
        <Button size="sm" variant="ghost" disabled={locked} onClick={() => void connection.importLocal()}>导入本机 Claude Code 登录</Button>
        {status && status.status !== "logged_out" && <Button size="sm" variant="ghost" disabled={locked} onClick={() => void connection.logout()}>退出登录</Button>}
      </div> : <div className="space-y-2">
        <p className="text-muted-foreground text-xs">{login.browser_opened ? "已打开浏览器。" : "请复制下方链接，在浏览器中打开。"}完成授权后，将页面显示的完整授权码粘贴到这里。</p>
        <div className="flex items-center gap-2">
          <Input aria-label="授权链接" readOnly value={login.authorization_url} className="min-w-0 text-xs" />
          <Button size="sm" variant="outline" onClick={() => void copyLink()}>复制授权链接</Button>
        </div>
        <label className="grid gap-1 text-xs">授权码
          <Input type="password" autoComplete="off" value={code} onChange={(event) => setCode(event.target.value)} placeholder="粘贴完整授权码" disabled={locked} />
        </label>
        <div className="flex gap-2">
          <Button size="sm" disabled={locked || !code.trim()} onClick={async () => { await connection.complete(code); setCode(""); }}>完成登录</Button>
          <Button size="sm" variant="ghost" disabled={locked} onClick={async () => { setCode(""); await connection.cancel(); }}>取消登录</Button>
        </div>
      </div>}
      {error && <Alert tone="destructive">{error}</Alert>}
    </div>
  );
}

/** 目录是模型能力来源；加载失败保留当前值并提供重试，不填入猜测列表。 */
export function ClaudeSubscriptionModelSelect({ connection, modelName, onSelect }: {
  connection: ClaudeSubscriptionConnectionState;
  modelName: string;
  onSelect: (model: ClaudeModelChoice) => void;
}) {
  const { modelsQuery, connected, busy } = connection;
  const models = modelsQuery.data?.models ?? [];
  const currentIsMissing = modelName && !models.some((model) => model.id === modelName);
  return <>
    <NativeSelect
      value={modelName}
      disabled={!connected || busy || modelsQuery.isFetching || models.length === 0}
      onChange={(event) => {
        const model = models.find((item) => item.id === event.target.value);
        if (model) onSelect(model);
      }}
    >
      <option value="">{!connected ? "请先登录 Claude" : modelsQuery.isFetching ? "正在读取模型列表…" : "请选择模型"}</option>
      {currentIsMissing && <option value={modelName}>{modelName}（当前配置）</option>}
      {models.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
    </NativeSelect>
    {connected && modelsQuery.isError && <span className="space-y-1">
      <span className="text-destructive block">{userErrorMessage(modelsQuery.error, "模型列表读取失败")}</span>
      <Button size="sm" variant="outline" onClick={() => void modelsQuery.refetch()}>重试模型列表</Button>
    </span>}
    {connected && modelsQuery.isSuccess && models.length === 0 && <span className="text-muted-foreground">账号未返回模型。<Button size="sm" variant="ghost" onClick={() => void modelsQuery.refetch()}>重试模型列表</Button></span>}
  </>;
}
