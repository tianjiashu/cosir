/**
 * 右侧信息面板组件（RightPanel）。
 *
 * 组合以下子组件：
 * - OutputsTab：Outputs 列表（任务产生的修改文件、文档）
 * - SourcesTab：Sources 列表（引用文档、上下文片段、规则文件）
 * - ContextBlock：上下文引用占位
 * - McpBlock：MCP 入口占位
 * - SubagentPanel：独立 Tab，展示选中委派子 Agent 的 timeline（复用真实 child 事件流）
 *
 * activeTab 为组件内受控 state，与 `delegationStore.selectedChildTurnId` 解耦：
 * 父 timeline 点击 delegation 行即选中 child turn，本面板通过 useEffect 自动切到
 * Subagent Tab；手动切回 outputs/sources 时清空选中态，并停留在用户所选 Tab（Sources
 * 因此可独立可达，不再被派生逻辑强制拉回 outputs）。
 *
 * 变更集（Changes）已从右侧栏移出，改由中央对话区上方的 ChangesDrawer 折叠呈现。
 *
 * @module components/layout/RightPanel
 */

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { FileText, Link2, Bot } from "lucide-react";
import { OutputsTab, type OutputItem } from "@/components/right-panel/OutputsTab";
import { SourcesTab, type SourceItem } from "@/components/right-panel/SourcesTab";
import { ContextBlock } from "@/components/right-panel/ContextBlock";
import { McpBlock } from "@/components/right-panel/McpBlock";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";
import { useEffect, useState } from "react";
import { useDelegationStore } from "@/stores/delegationStore";

/** Mock Outputs 数据（第一版静态数据）。 */
const MOCK_OUTPUTS: OutputItem[] = [
  {
    id: "out-1",
    name: "rules/Agent-client-code-guide.md",
    description: "Client code development guide.",
  },
  {
    id: "out-2",
    name: "AGENTS.md",
    description: "Updated project routing guide.",
  },
];

/** Mock Sources 数据（第一版静态数据）。 */
const MOCK_SOURCES: SourceItem[] = [
  {
    id: "src-1",
    name: "docs/desktop-client-development-plan.md",
    description: "桌面客户端开发规范与实施清单",
  },
  {
    id: "src-2",
    name: "rules/Agent-code-guide.md",
    description: "Backend code development guide.",
  },
  {
    id: "src-3",
    name: "docs/ui-guidelines.md",
    description: "UI visual and component guide.",
  },
];

/**
 * 右侧信息面板组件。
 *
 * 宽度由外层可拖拽 Panel 决定（本组件撑满容器），
 * 通过 Tabs 切换 Outputs/Sources/Subagent，展示各标签页内容。
 *
 * activeTab 为组件内受控 state（与 `delegationStore.selectedChildTurnId` 解耦）：
 * - 父 timeline 点击 delegation 行选中 child turn 时，useEffect 自动将 activeTab 切到 "subagent"；
 * - 手动切到 outputs/sources 时清空选中态，并保留用户当前所选 Tab（Sources 因此可独立可达）；
 * - 选中态清空后不强制改回 outputs，避免覆盖用户手动选择。
 *
 * 注意：Changes 已移至中央对话区上方的 ChangesDrawer，本面板不再含 Changes Tab。
 *
 * @returns 右侧信息面板。
 */
export function RightPanel() {
  const selectedChildTurnId = useDelegationStore((state) => state.selectedChildTurnId);
  const clearSelection = useDelegationStore((state) => state.clearSelection);

  // activeTab 为受控 state，与 selectedChildTurnId 解耦。初始值：选中态非空时落到
  // "subagent"，否则回落 "outputs"。后续用户手动切换由 handleTabChange 写入 state，
  // 不再被派生值强制覆盖（修复 Sources tab 不可达：原派生逻辑在未选中时永远返回
  // "outputs"，导致点击 sources 被 clearSelection 拉回 outputs）。
  const [activeTab, setActiveTab] = useState<string>(
    selectedChildTurnId ? "subagent" : "outputs",
  );

  // 选中态变化时的派生同步：选中 child turn → 自动切到 subagent；
  // 清空选中态时不强制改回 outputs，保留用户当前所选 Tab（如 sources），
  // 避免覆盖手动选择。
  useEffect(() => {
    if (selectedChildTurnId) {
      setActiveTab("subagent");
    }
  }, [selectedChildTurnId]);

  // Tabs 的 value 语义是 tab 名（"subagent"/"outputs"/"sources"），不是 turn id。
  // 手动切换时写入 activeTab state；切到非 subagent tab 时清空选中态，使 Subagent
  // Tab 不被置灰悬停，并保持 tab 与选中态解耦后的用户选择。
  const handleTabChange = (value: string) => {
    setActiveTab(value);
    if (value !== "subagent") {
      clearSelection();
    }
  };

  return (
    <aside className="flex h-full w-full min-w-0 flex-col bg-background">
      <Tabs
        value={activeTab}
        onValueChange={handleTabChange}
        className="flex h-full flex-col"
      >
        {/* Tab 切换栏 */}
        {/* calc 用于抵消父容器 mx-2 左右外边距，使 Tab 栏宽度与内容区对齐，非通用语义 */}
        {/* eslint-disable-next-line tailwind/no-arbitrary-value */}
        <TabsList className="mx-2 mt-2 flex min-w-0 overflow-hidden w-[calc(100%-1rem)]">
          <TabsTrigger value="outputs" className="min-w-0 flex-1 gap-1.5 truncate text-xs">
            <FileText className="h-3.5 w-3.5 shrink-0" />
            Outputs
          </TabsTrigger>
          <TabsTrigger value="sources" className="min-w-0 flex-1 gap-1.5 truncate text-xs">
            <Link2 className="h-3.5 w-3.5 shrink-0" />
            Sources
          </TabsTrigger>
          <TabsTrigger value="subagent" className="min-w-0 flex-1 gap-1.5 truncate text-xs">
            <Bot className="h-3.5 w-3.5 shrink-0" />
            Subagent
          </TabsTrigger>
        </TabsList>

        {/* 内容区：可滚动 */}
        <div className="flex-1 overflow-hidden">
          {/* Outputs Tab：使用独立子组件 */}
          <TabsContent value="outputs" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-2">
                <OutputsTab items={MOCK_OUTPUTS} />

                <Separator className="my-3" />
              </div>
            </ScrollArea>
          </TabsContent>

          {/* Sources Tab：使用独立子组件 */}
          <TabsContent value="sources" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-2">
                <SourcesTab items={MOCK_SOURCES} />

                <Separator className="my-3" />

                {/* 预留扩展区块：使用独立子组件 */}
                <ContextBlock />
                <McpBlock />
              </div>
            </ScrollArea>
          </TabsContent>

          {/* Subagent Tab：独立展示选中委派子 Agent 的 timeline（从 SourcesTab 子树移出） */}
          <TabsContent value="subagent" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-2">
                <SubagentPanel />
              </div>
            </ScrollArea>
          </TabsContent>
        </div>
      </Tabs>
    </aside>
  );
}
