import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon, SearchIcon } from "lucide-react";

import { SkillDetail } from "@/components/configuration/skill-detail";
import { SkillImportMenu } from "@/components/configuration/skill-import-menu";
import { SkillRootGroup } from "@/components/configuration/skill-root-group";
import { invalidateSkills, useSkillActions } from "@/components/configuration/use-skill-actions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { getSkillCatalog, ownScope, type ExtensionScopeId, type SkillCatalogView } from "@/lib/api/extensions";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { diffSkillCatalogs } from "@/lib/skills/skill-catalog-diff";
import { bulkToggleTargets, searchSkillGroups, type SearchedSkillGroup } from "@/lib/skills/skill-catalog-search";
import { effectiveRootLabels } from "@/lib/skills/skill-directory";

/**
 * 一个作用域的 Skills 页：`skillName` 有值时显示这个 Skill 的详情页（路由 `…/settings/skills/:skillName`），
 * 否则显示按根目录（作用域 × 身份）分组的目录。`workspaceId` 为 `null` 时管理系统级；否则管理该工作区这一层，
 * 继承来的系统级分组只读。
 */
export function SkillConfigurationPanel({ workspaceId, skillName }: { workspaceId: ExtensionScopeId; skillName?: string }) {
  if (skillName !== undefined) return <SkillDetail key={skillName} workspaceId={workspaceId} name={skillName} />;
  return <SkillCatalogPage workspaceId={workspaceId} />;
}

/**
 * 分组目录页。用户来这里要回答：Agent 现在能用哪些、代价多大（顶部摘要）；每个 Skill 还有谁能看到、为什么
 * 某个没生效（按身份分卡，越靠上越优先，被覆盖的写明被谁覆盖）；哪些该开该关（搜索、对搜索结果批量启停）；
 * 具体某个是什么（进详情）。谁生效、谁被覆盖全部取自后端视图，这里只做展示与搜索。
 *
 * 后端每次请求都现读目录，滞后的只有页面缓存：用其他工具装完 Skill 回来，点「重新扫描」重读。
 * 已开着的会话不受影响（系统提示词在会话创建时固定），配置变化的通知以后统一做。
 */
function SkillCatalogPage({ workspaceId }: { workspaceId: ExtensionScopeId }) {
  const pageScope = ownScope(workspaceId);
  const actions = useSkillActions(workspaceId);
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const catalogQuery = useQuery({
    queryKey: queryKeys.skillCatalog(workspaceId),
    queryFn: ({ signal }) => getSkillCatalog(workspaceId, { signal }),
  });

  const groups = catalogQuery.data?.groups ?? [];
  const searching = query.trim() !== "";
  const searched = searchSkillGroups(groups, query);
  const own = searched.filter(({ group }) => group.scope === pageScope);
  // 继承来的空目录在这一页动不了，系统页已经显示，这里不列。
  const inherited = searched.filter(({ group }) => group.scope !== pageScope && (group.skills.length > 0 || group.skipped.length > 0));
  const bulk = bulkToggleTargets(searched, pageScope);
  const hasSkills = groups.some((group) => group.skills.length > 0);
  const effectiveLabels = effectiveRootLabels(groups, pageScope);
  const importLocation = groups.find((group) => group.scope === pageScope && group.kind === "cosir")?.location;

  // 一并失效输入框选择器与详情页：它们读的是同一批目录。
  async function rescan() {
    const before = catalogQuery.data;
    await invalidateSkills(queryClient, workspaceId);
    const after = queryClient.getQueryData<SkillCatalogView>(queryKeys.skillCatalog(workspaceId));
    if (!before || !after || queryClient.getQueryState(queryKeys.skillCatalog(workspaceId))?.status === "error") return;
    const { added, removed } = diffSkillCatalogs(before, after);
    notify.success(added === 0 && removed === 0 ? "扫描完成，没有变化" : `扫描完成：新增 ${added} 个，移除 ${removed} 个`);
  }

  const renderGroups = (groups: SearchedSkillGroup[], editable: boolean) => groups.map(({ group, skills }) => (
    <SkillRootGroup
      key={`${group.scope}:${group.kind}`}
      group={group}
      skills={skills}
      workspaceId={workspaceId}
      editable={editable}
      searching={searching}
      busy={actions.busy}
      effectiveLabels={effectiveLabels}
      onToggleSkill={actions.toggle}
    />
  ));

  return (
    <section className="space-y-4" aria-labelledby="skill-configuration-heading">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-1">
          <h2 id="skill-configuration-heading" className="text-lg font-semibold">Skills</h2>
          {catalogQuery.data && <AgentCatalogSummary view={catalogQuery.data} workspaceId={workspaceId} />}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" size="sm" disabled={catalogQuery.isFetching} onClick={() => void rescan()}>
            <RefreshCwIcon className={catalogQuery.isFetching ? "animate-spin" : undefined} />重新扫描
          </Button>
          <SkillImportMenu workspaceId={workspaceId} location={importLocation} />
        </div>
      </header>

      {catalogQuery.isPending ? (
        <LoadingState label="正在读取 Skills…" />
      ) : catalogQuery.isError ? (
        <ErrorState
          message={userErrorMessage(catalogQuery.error, "读取 Skill 目录失败")}
          onRetry={() => void catalogQuery.refetch()}
          retryLabel="重新读取"
        />
      ) : (
        <>
          {hasSkills && <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-48 flex-1">
              <SearchIcon className="text-muted-foreground pointer-events-none absolute top-2 left-2.5 size-4" aria-hidden="true" />
              <Input className="pl-8 text-sm" aria-label="搜索 Skill" placeholder="搜索名称或描述…" value={query} onChange={(event) => setQuery(event.target.value)} />
            </div>
            {searching && bulk.enabled.length > 0 && (
              <Button variant="outline" size="sm" disabled={actions.busy} onClick={() => actions.setEnabled(bulk.enabled, false)}>停用这 {bulk.enabled.length} 个</Button>
            )}
            {searching && bulk.disabled.length > 0 && (
              <Button variant="outline" size="sm" disabled={actions.busy} onClick={() => actions.setEnabled(bulk.disabled, true)}>启用这 {bulk.disabled.length} 个</Button>
            )}
          </div>}

          {searched.length === 0 && <p className="text-muted-foreground py-6 text-center text-sm">没有符合条件的 Skill</p>}
          {/* 后端已按优先级排好：本层在前，同层专属在前。工作区页分「本工作区」与「继承自系统级」两节。 */}
          {workspaceId === null ? (
            <div className="space-y-3">{renderGroups(own, true)}</div>
          ) : (
            <>
              {own.length > 0 && (
                <div className="space-y-3">
                  <h3 className="text-muted-foreground text-xs font-medium">本工作区</h3>
                  {renderGroups(own, true)}
                </div>
              )}
              {inherited.length > 0 && (
                <div className="space-y-3">
                  <h3 className="text-muted-foreground text-xs font-medium">继承自系统级 · 在系统设置中管理</h3>
                  {renderGroups(inherited, false)}
                </div>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}

/** Agent 实际看到的目录条数与体量：数字来自后端，用提示词同一个渲染函数估算。 */
function AgentCatalogSummary({ view, workspaceId }: { view: SkillCatalogView; workspaceId: ExtensionScopeId }) {
  return (
    <div className="text-muted-foreground space-y-0.5 text-sm">
      <p>
        {workspaceId === null ? "Agent 能用 " : "在这个工作区里，Agent 能用 "}
        <span className="text-foreground font-medium">{view.agent_skills}</span> 个 Skill
        {view.agent_skills > 0 && <>，目录约 <span className="text-foreground font-medium">{view.agent_tokens.toLocaleString("zh-CN")}</span> token，每个新会话的系统提示词都会带上</>}
        {workspaceId === null && "（工作区还可以另加自己的）"}。
      </p>
      <p className="text-xs">改动对新会话生效，已开着的会话沿用开始时的目录。</p>
    </div>
  );
}
