/**
 * 右侧信息面板组件（RightPanel）。
 *
 * 组合以下子组件：
 * - OutputsTab：Outputs 列表（任务产生的修改文件、文档）
 * - SourcesTab：Sources 列表（引用文档、上下文片段、规则文件）
 * - ContextBlock：上下文引用占位
 * - McpBlock：MCP 入口占位
 * - SubagentPanel：选中委派子 Agent 的 timeline 展示（复用真实 child 事件流）
 *
 * 变更集（Changes）已从右侧栏移出，改由中央对话区上方的 ChangesDrawer 折叠呈现。
 *
 * @module components/layout/RightPanel
 */

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { FileText, Link2 } from "lucide-react";
import { OutputsTab, type OutputItem } from "@/components/right-panel/OutputsTab";
import { SourcesTab, type SourceItem } from "@/components/right-panel/SourcesTab";
import { ContextBlock } from "@/components/right-panel/ContextBlock";
import { McpBlock } from "@/components/right-panel/McpBlock";
import { SubagentPanel } from "@/components/right-panel/SubagentPanel";

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
 * 通过 Tabs 切换 Outputs/Sources，展示各标签页内容。
 * 注意：Changes 已移至中央对话区上方的 ChangesDrawer，本面板不再含 Changes Tab。
 *
 * @returns 右侧信息面板。
 */
export function RightPanel() {
  return (
    <aside className="flex h-full w-full min-w-0 flex-col bg-background">
      <Tabs defaultValue="outputs" className="flex h-full flex-col">
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
                <SubagentPanel />
              </div>
            </ScrollArea>
          </TabsContent>
        </div>
      </Tabs>
    </aside>
  );
}
