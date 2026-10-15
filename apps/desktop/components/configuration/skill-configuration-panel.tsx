"use client";

import { useEffect, useRef, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { Loader2Icon, PlusIcon, PuzzleIcon, Trash2Icon } from "lucide-react";
import { Streamdown } from "streamdown";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Switch } from "@/components/ui/switch";
import { markdownPlugins } from "@/components/markdown-rendering-config";
import {
  deleteSystemSkill,
  getSystemSkill,
  getSystemSkills,
  importSystemSkill,
  saveEnabledSkills,
  type SystemSkill,
  type SystemSkillDocument,
} from "@/lib/api/system-extensions";

/** 系统目录 Skill 的导入、启停与只读详情管理。 */
export function SkillConfigurationPanel() {
  const detailRequestId = useRef(0);
  const importInFlight = useRef(false);
  const [skills, setSkills] = useState<SystemSkill[]>([]);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busySkill, setBusySkill] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState<SystemSkillDocument | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  async function load() {
    setLoadState("loading");
    setLoadError(null);
    try {
      setSkills(await getSystemSkills());
      setLoadState("ready");
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "读取 Skill 列表失败");
      setLoadState("error");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function toggleSkill(skill: SystemSkill) {
    setBusySkill(skill.name);
    setLoadError(null);
    setNotice(null);
    const enabledNames = skills
      .filter((item) => item.name === skill.name ? !item.enabled : item.enabled)
      .map((item) => item.name);
    try {
      setSkills(await saveEnabledSkills(enabledNames));
      setNotice(`已${skill.enabled ? "停用" : "启用"} ${skill.name}`);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "保存 Skill 状态失败");
    } finally {
      setBusySkill(null);
    }
  }

  async function selectAndImport(sourceType: "directory" | "archive") {
    if (importInFlight.current) return;
    importInFlight.current = true;
    setImporting(true);
    setLoadError(null);
    setNotice(null);
    try {
      const sourcePath = sourceType === "directory"
        ? await open({ directory: true, multiple: false, title: "选择要导入的 Skill 文件夹" })
        : await open({
            multiple: false,
            title: "选择要导入的 Skill ZIP",
            filters: [{ name: "Skill ZIP", extensions: ["zip"] }],
          });
      if (typeof sourcePath !== "string") return;
      const skill = await importSystemSkill(sourcePath, sourceType);
      setSkills((current) =>
        [...current.filter((item) => item.name !== skill.name), skill].sort((a, b) =>
          a.name.localeCompare(b.name),
        ),
      );
      setNotice(`已导入 ${skill.name}，文件已复制到系统 Skill 目录。`);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "导入 Skill 失败");
    } finally {
      importInFlight.current = false;
      setImporting(false);
    }
  }

  async function showDetails(skill: SystemSkill) {
    const requestId = ++detailRequestId.current;
    setDetailOpen(true);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const document = await getSystemSkill(skill.name);
      if (requestId === detailRequestId.current) setDetail(document);
    } catch (error) {
      if (requestId === detailRequestId.current) {
        setDetailError(error instanceof Error ? error.message : "读取 Skill 详情失败");
      }
    } finally {
      if (requestId === detailRequestId.current) setDetailLoading(false);
    }
  }

  async function removeSkill(skill: SystemSkill) {
    if (!window.confirm(`删除 Skill“${skill.name}”及其系统目录文件？`)) return;
    setBusySkill(skill.name);
    setLoadError(null);
    setNotice(null);
    try {
      await deleteSystemSkill(skill.name);
      setSkills((current) => current.filter((item) => item.name !== skill.name));
      setNotice(`已删除 ${skill.name}`);
      if (detail?.name === skill.name) setDetailOpen(false);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "删除 Skill 失败");
    } finally {
      setBusySkill(null);
    }
  }

  return (
    <section className="space-y-5" aria-labelledby="skill-configuration-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <h2 id="skill-configuration-heading" className="text-lg font-semibold">Skills</h2>
        </div>
        <div className="flex gap-2">
          <Button onClick={() => void selectAndImport("directory")} disabled={loadState !== "ready" || importing}>
            {importing ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
            导入 Skill 文件夹
          </Button>
          <Button variant="outline" onClick={() => void selectAndImport("archive")} disabled={loadState !== "ready" || importing}>
            {importing ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}
            导入 ZIP
          </Button>
        </div>
      </header>

      {notice && <Alert tone="success">{notice}</Alert>}
      {loadError && loadState !== "error" && <Alert tone="destructive">{loadError}</Alert>}
      {loadState === "loading" ? (
        <LoadingState label="正在读取 Skills…" />
      ) : loadState === "error" ? (
        <ErrorState message={loadError ?? "读取 Skill 列表失败"} onRetry={() => void load()} retryLabel="重新读取" />
      ) : skills.length === 0 ? (
        <EmptyState
          icon={<PuzzleIcon />}
          title="还没有 Skill"
          description="导入本机 Skill 文件夹后，可以在这里启用、停用或查看内容。"
        />
      ) : (
        <div className="space-y-3">
          {skills.map((skill) => (
            <Card key={skill.name} className="flex flex-wrap items-start gap-3 p-4">
              <PuzzleIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" className="truncate text-left text-sm font-medium hover:underline" onClick={() => void showDetails(skill)}>{skill.name}</button>
                  <Badge size="sm" tone={skill.enabled ? "primary" : "neutral"}>
                    {skill.enabled ? "已启用" : "已停用"}
                  </Badge>
                </div>
                <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">{skill.description}</p>
                <Button variant="link" size="sm" className="mt-1 h-auto px-0 py-0 text-xs" onClick={() => void showDetails(skill)}>查看说明</Button>
              </div>
              <Switch checked={skill.enabled} aria-label={`${skill.enabled ? "停用" : "启用"} Skill ${skill.name}`} disabled={loadState !== "ready" || busySkill === skill.name || importing} onClick={() => void toggleSkill(skill)} />
              <Button variant="ghost" size="icon" aria-label={`删除 Skill ${skill.name}`} disabled={loadState !== "ready" || busySkill === skill.name || importing} onClick={() => void removeSkill(skill)}><Trash2Icon /></Button>
            </Card>
          ))}
        </div>
      )}

      <Dialog open={detailOpen} onOpenChange={(open) => {
        setDetailOpen(open);
        if (!open) detailRequestId.current += 1;
      }}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{detail?.name ?? "Skill 说明"}</DialogTitle>
            <DialogDescription>{detail?.description ?? "只读预览 Skill.md 内容。"}</DialogDescription>
          </DialogHeader>
          {detailLoading ? (
            <LoadingState label="正在读取说明…" className="py-8" />
          ) : detailError ? (
            <Alert tone="destructive">{detailError}</Alert>
          ) : detail ? (
            <div className="aui-md max-h-[60vh] overflow-y-auto rounded-lg border bg-muted/20 p-4 leading-6">
              <Streamdown mode="static" plugins={markdownPlugins} shikiTheme={["github-light", "github-dark"]} linkSafety={{ enabled: true }} mermaid={{ config: { securityLevel: "strict" } }} className="!space-y-3">
                {detail.content}
              </Streamdown>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </section>
  );
}
