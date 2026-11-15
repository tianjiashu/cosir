import { createBrowserRouter, Navigate, useMatches } from "react-router-dom";

import { AppLayout, parseRouteId, useAppOutlet, type RouteHandle } from "@/components/layout/app-layout";
import { SessionObserverPage } from "@/components/session-observer/session-observer-page";
import { NewConversation } from "@/components/new-conversation/new-conversation";
import {
  configurationPath,
  resolveConfigurationTab,
  SYSTEM_CONFIGURATION_TABS,
  WORKSPACE_CONFIGURATION_TABS,
} from "@/components/configuration/configuration-tabs";
import { SystemConfigurationPage } from "@/components/configuration/system-configuration-page";
import { EmptyState } from "@/components/ui/state-panel";
import { WorkspaceConfigurationPage } from "@/components/configuration/workspace-configuration-page";

/**
 * 应用路由表（react-router data router）。
 *
 * AppLayout 是唯一的布局路由，子路由只渲染主区域内容，并用 handle.section 声明所属区域，
 * 供布局决定面包屑、侧栏高亮与会话页保活。会话页本身由布局渲染（保活需要），
 * 因此 `sessions/:sessionId` 只做标识校验。新增页面：在这里加一条子路由并声明 section。
 */

const conversation: RouteHandle = { section: "conversation", files: true };
const configuration: RouteHandle = { section: "configuration" };
const workspaceConfiguration: RouteHandle = { section: "workspace-configuration" };
const observer: RouteHandle = { section: "observer" };

/** 当前叶子路由的参数（布局路由下统一从叶子匹配读取）。 */
function useLeafParams() {
  return useMatches().at(-1)?.params ?? {};
}

function NewConversationRoute() {
  const { catalog, actions } = useAppOutlet();
  return (
    <NewConversation
      workspaces={catalog.workspaces}
      catalogLoading={catalog.status === "loading" && catalog.workspaces.length === 0}
      selectedWorkspaceId={catalog.selectedWorkspaceId}
      unstartedSession={catalog.selectedWorkspace?.unstartedSession ?? null}
      onWorkspaceChange={catalog.selectWorkspace}
      onAddWorkspace={actions.addWorkspaceFromFolder}
      addingWorkspace={actions.addingWorkspace}
    />
  );
}

function SessionRoute() {
  return parseRouteId(useLeafParams().sessionId) !== null ? null : <EmptyState title="会话标识无效" description="请从侧栏重新选择会话。" className="m-6" />;
}

function SystemConfigurationRoute() {
  const params = useLeafParams();
  const tab = resolveConfigurationTab(SYSTEM_CONFIGURATION_TABS, params);
  return tab
    ? <SystemConfigurationPage tab={tab.id} skillName={params.skillName} />
    : <Navigate to={configurationPath(null, SYSTEM_CONFIGURATION_TABS[0].id)} replace />;
}

function WorkspaceConfigurationRoute() {
  const { catalog } = useAppOutlet();
  const params = useLeafParams();
  const workspaceId = parseRouteId(params.workspaceId);
  if (workspaceId === null) {
    return <EmptyState title="工作区标识无效" description="请从侧栏重新选择工作区。" className="m-6" />;
  }
  const tab = resolveConfigurationTab(WORKSPACE_CONFIGURATION_TABS, params);
  if (!tab) return <Navigate to={configurationPath(workspaceId, WORKSPACE_CONFIGURATION_TABS[0].id)} replace />;
  const workspaceName = catalog.workspaces.find((workspace) => workspace.workspace_id === workspaceId)?.name ?? "当前工作区";
  return <WorkspaceConfigurationPage key={workspaceId} workspaceId={workspaceId} workspaceName={workspaceName} tab={tab.id} skillName={params.skillName} />;
}

function NotFoundRoute() {
  return <EmptyState title="页面不存在" description="请从侧栏选择要打开的内容。" className="m-6" />;
}

export function createAppRouter() {
  return createBrowserRouter([
    {
      path: "/",
      element: <AppLayout />,
      children: [
        { index: true, element: <NewConversationRoute />, handle: conversation },
        { path: "sessions/:sessionId", element: <SessionRoute />, handle: conversation },
        { path: "configuration", element: <SystemConfigurationRoute />, handle: configuration },
        { path: "configuration/:tab", element: <SystemConfigurationRoute />, handle: configuration },
        { path: "configuration/skills/:skillName", element: <SystemConfigurationRoute />, handle: configuration },
        { path: "workspaces/:workspaceId/configuration", element: <WorkspaceConfigurationRoute />, handle: workspaceConfiguration },
        { path: "workspaces/:workspaceId/configuration/:tab", element: <WorkspaceConfigurationRoute />, handle: workspaceConfiguration },
        { path: "workspaces/:workspaceId/configuration/skills/:skillName", element: <WorkspaceConfigurationRoute />, handle: workspaceConfiguration },
        { path: "session-observer", element: <SessionObserverPage />, handle: observer },
        { path: "*", element: <NotFoundRoute />, handle: { section: "conversation" } },
      ],
    },
  ]);
}
