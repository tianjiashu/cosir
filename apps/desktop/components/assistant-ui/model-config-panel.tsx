"use client";

import { useState } from "react";
import { Settings2Icon } from "lucide-react";

import { ModelConfigManager } from "@/components/model/model-config-manager";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { confirmDiscardUnsaved } from "@/lib/unsaved-changes";

/**
 * composer 模型下拉里的「模型设置」入口与对话框；内容由 ModelConfigManager 承担，
 * 与配置中心「模型」标签共用。
 *
 * 未保存守卫使用独立的 "dialog" scope：对话框关闭只询问自己的表单，不牵连配置中心的路由拦截。
 * 对话框内容关闭即卸载，放弃的修改不会在下次打开时重现。
 */
export function ModelConfigPanel() {
  const [open, setOpen] = useState(false);

  async function closeDialog() {
    if (!(await confirmDiscardUnsaved("dialog"))) return;
    setOpen(false);
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-muted-foreground hover:text-foreground inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs"
      >
        <Settings2Icon className="size-3.5" />
        模型设置
      </button>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (nextOpen) setOpen(true);
          else void closeDialog();
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>模型设置</DialogTitle>
            <DialogDescription>填写 OpenAI 兼容接口的连接配置，模型会出现在选择列表中。</DialogDescription>
          </DialogHeader>
          <ModelConfigManager unsavedScope="dialog" variant="dialog" />
        </DialogContent>
      </Dialog>
    </>
  );
}
