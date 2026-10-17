import { createBrowserRouter, Navigate, useMatches } from "react-router-dom";

import { AppLayout, useAppOutlet, type RouteHandle } from "@/components/layout/app-layout";
import { ContextObserverPage } from "@/components/context-observer-page";
import { NewConversation } from "@/components/new-conversation";
import {
  findSettingsTab,
  SYSTEM_SETTINGS_TABS,
  WORKSPACE_SETTINGS_TABS,
} from "@/components/settings/settings-tabs";
import { SystemConfigurationPage } from "@/components/system-configuration-page";
import { EmptyState } from "@/components/ui/state-panel";
import { WorkspaceConfigurationPage } from "@/components/workspace-configuration-page";

/**
 * 应用路由表（react-router data router）。
 *
 * AppLayout 是唯一的布局路由，子路由只渲染主区域内容，并用 handle.section 声明所属区域，
 * 供布局决定面包屑、侧栏高亮与任务页保活。任务页本身由布局渲染（保活需要），
 * 因此 `tasks/:taskId` 只做标识校验。新增页面：在这里加一条子路由并声明 section。
 */

const conversation: RouteHandle = { section: "conversation", files: true };
const settings: RouteHandle = { section: "settings" };
const workspaceSettings: RouteHandle = { section: "workspace-settings" };
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
      workspacesLoading={catalog.status === "loading"}
      selectedWorkspaceId={catalog.selectedWorkspaceId}
      onWorkspaceChange={catalog.selectWorkspace}
      onWorkspaceCreated={catalog.load}
      onStarted={actions.startConversation}
      onTaskListChanged={() => void catalog.load()}
    />
  );
}

function TaskRoute() {
  const taskId = Number(useLeafParams().taskId);
  return Number.isInteger(taskId) && taskId > 0 ? null : <EmptyState title="任务标识无效" description="请从侧栏重新选择任务。" className="m-6" />;
}

function SystemSettingsRoute() {
  const tab = findSettingsTab(SYSTEM_SETTINGS_TABS, useLeafParams().tab);
  return tab ? <SystemConfigurationPage tab={tab.id} /> : <Navigate to={`/settings/${SYSTEM_SETTINGS_TABS[0].id}`} replace />;
}

function WorkspaceSettingsRoute() {
  const { catalog } = useAppOutlet();
  const params = useLeafParams();
  const workspaceId = Number(params.workspaceId);
  if (!Number.isInteger(workspaceId) || workspaceId <= 0) {
    return <EmptyState title="工作区标识无效" description="请从侧栏重新选择工作区。" className="m-6" />;
  }
  const tab = findSettingsTab(WORKSPACE_SETTINGS_TABS, params.tab);
  if (!tab) return <Navigate to={`/workspaces/${workspaceId}/settings/${WORKSPACE_SETTINGS_TABS[0].id}`} replace />;
  const workspaceName = catalog.workspaces.find((workspace) => workspace.workspace_id === workspaceId)?.name ?? "当前工作区";
  return <WorkspaceConfigurationPage key={workspaceId} workspaceId={workspaceId} workspaceName={workspaceName} tab={tab.id} />;
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
        { path: "tasks/:taskId", element: <TaskRoute />, handle: conversation },
        { path: "settings", element: <SystemSettingsRoute />, handle: settings },
        { path: "settings/:tab", element: <SystemSettingsRoute />, handle: settings },
        { path: "workspaces/:workspaceId/settings", element: <WorkspaceSettingsRoute />, handle: workspaceSettings },
        { path: "workspaces/:workspaceId/settings/:tab", element: <WorkspaceSettingsRoute />, handle: workspaceSettings },
        { path: "context-observer", element: <ContextObserverPage />, handle: observer },
        { path: "*", element: <NotFoundRoute />, handle: { section: "conversation" } },
      ],
    },
  ]);
}
