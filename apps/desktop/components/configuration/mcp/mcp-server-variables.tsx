import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRoundIcon, Loader2Icon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { setEnvironmentVariables } from "@/lib/api/configuration";
import type { MCPServer, MCPVariableStatus } from "@/lib/api/extensions";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { notify } from "@/lib/notify";
import { invalidateMcpServers } from "@/lib/query/extension-invalidation";

const SOURCE_LABELS: Record<MCPVariableStatus["source"], string> = { unset: "未设置", cosir: "已设置", shell: "来自 shell" };

function variableTone(variable: MCPVariableStatus): "success" | "neutral" | "warning" {
  if (variable.source !== "unset") return "success";
  return variable.has_default ? "neutral" : "warning";
}

function variableTitle(variable: MCPVariableStatus): string {
  if (variable.source === "shell") return "值来自你的 shell 环境，优先于 cosir 的设置；要改请在 shell 里改";
  if (variable.source === "cosir") return "值保存在 cosir 的系统 .env 里，可以修改";
  return variable.has_default ? "未设置，使用默认值" : "未设置，连接会失败";
}

/**
 * 一个 MCP 服务引用的环境变量（`${NAME}`）：逐个标出值从哪来，没设置的可以填写、存在 cosir 里的可以修改。
 *
 * 值写进系统 `.env`（任何作用域的配置页都写同一处，只写不读）；配置文件里只留变量名，工作区配置随仓库共享也
 * 不带密钥。来自用户 shell 的变量启动时优先于 `.env`，这里改不动，只说明去哪改。保存后交给 `onSaved`
 * （配置页据此重测引用这些变量的服务：「测试通过」认的是定义，不认变量值，不重测就看不出新值对不对）。
 */
export function MCPServerVariables({ server, onSaved }: { server: MCPServer; onSaved: (names: string[]) => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const editable = server.variables.filter((variable) => variable.source !== "shell");
  const needsFilling = editable.some((variable) => variable.source === "unset");

  const mutation = useMutation({
    mutationFn: (next: Record<string, string>) => setEnvironmentVariables(next).then(() => Object.keys(next)),
    onSuccess: (names) => {
      // 变量是系统级事实：所有作用域列表里的变量来源与主状态都随之变化。
      void invalidateMcpServers(queryClient, null);
      setOpen(false);
      notify.success("变量已保存");
      onSaved(names);
    },
    onError: (cause) => setError(userErrorMessage(cause, "保存变量失败")),
  });

  if (server.variables.length === 0) return null;

  function openDialog() {
    setValues({});
    setError(null);
    setOpen(true);
  }

  function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const filled = Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ""));
    if (Object.keys(filled).length === 0) {
      setError("请至少填写一个变量");
      return;
    }
    mutation.mutate(filled);
  }

  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">引用变量</span>
      {server.variables.map((variable) => (
        <Badge
          key={variable.name}
          size="sm"
          tone={variableTone(variable)}
          title={variableTitle(variable)}
        >
          <span className="font-mono">{variable.name}</span>
          <span className="ml-1">{variable.source === "unset" && variable.has_default ? "默认值" : SOURCE_LABELS[variable.source]}</span>
        </Badge>
      ))}
      {editable.length > 0 && (
        <Button variant="ghost" size="xs" onClick={openDialog}>
          <KeyRoundIcon />{needsFilling ? "填写" : "修改"}
        </Button>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>设置 {server.name} 引用的变量</DialogTitle>
          </DialogHeader>
          <form onSubmit={save} className="space-y-3">
            <p className="text-muted-foreground text-xs">值保存在本机的系统 .env 里，配置文件中只保留变量名；保存后会重新测试引用它们的服务。</p>
            {/* 保存期间只读：成功回执即关闭对话框，等待期间的输入没有去处。 */}
            <fieldset disabled={mutation.isPending} className="m-0 min-w-0 space-y-3 border-0 p-0">
              {editable.map((variable) => (
                <label key={variable.name} className="block space-y-1.5 text-sm">
                  <span className="font-mono">{variable.name}{variable.source === "unset" && variable.has_default && <span className="text-muted-foreground ml-2 font-sans text-xs">可留空，使用默认值</span>}</span>
                  <Input
                    type="password"
                    autoComplete="off"
                    placeholder={variable.source === "cosir" ? "已设置，留空则保持不变" : undefined}
                    value={values[variable.name] ?? ""}
                    onChange={(event) => setValues((current) => ({ ...current, [variable.name]: event.target.value }))}
                  />
                </label>
              ))}
            </fieldset>
            {error && <Alert tone="destructive">{error}</Alert>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setOpen(false)}>取消</Button>
              <Button type="submit" disabled={mutation.isPending}>{mutation.isPending && <Loader2Icon className="animate-spin" />}保存</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
