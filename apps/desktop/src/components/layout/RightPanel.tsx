/**
 * 右侧信息面板组件（RightPanel）。
 *
 * 组合以下子组件：
 * - OutputsTab：Outputs 列表（任务产生的修改文件、文档）
 * - SourcesTab：Sources 列表（引用文档、上下文片段、规则文件）
 * - ContextBlock：上下文引用占位
 * - ConversationTraceBlock：当前对话 trace 诊断入口
 * - McpBlock：MCP 入口占位
 * - SubagentBlock：subagent 分组占位
 *
 * 第一版使用静态 mock 数据。
 *
 * @module components/layout/RightPanel
 */

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { FileText, GitCompareArrows, Link2 } from "lucide-react";
import { OutputsTab, type OutputItem } from "@/components/right-panel/OutputsTab";
import { SourcesTab, type SourceItem } from "@/components/right-panel/SourcesTab";
import { ChangesTab } from "@/components/right-panel/ChangesTab";
import { ContextBlock } from "@/components/right-panel/ContextBlock";
import { ConversationTraceBlock } from "@/components/right-panel/ConversationTraceBlock";
import { McpBlock } from "@/components/right-panel/McpBlock";
import { SubagentBlock } from "@/components/right-panel/SubagentBlock";
import { useTaskStore } from "@/stores/taskStore";

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
 * RightPanel 组件属性。
 */
interface RightPanelProps {
  /** 打开日志页面。 */
  onOpenLogs: () => void;
}

/**
 * 右侧信息面板组件。
 *
 * 宽度由外层可拖拽 Panel 决定（本组件撑满容器），
 * 通过 Tabs 切换 Outputs/Sources，底部展示预留扩展区块。
 *
 * @param props - 组件属性。
 * @returns 右侧信息面板。
 */
export function RightPanel({ onOpenLogs }: RightPanelProps) {
  const activeTaskId = useTaskStore((state) => state.activeTaskId);

  return (
    <aside className="flex h-full w-full min-w-0 flex-col bg-background">
      <Tabs defaultValue="outputs" className="flex h-full flex-col">
        {/* Tab 切换栏 */}
        <TabsList className="mx-2 mt-2 w-[calc(100%-1rem)]">
          <TabsTrigger value="outputs" className="gap-1.5 text-xs">
            <FileText className="h-3.5 w-3.5" />
            Outputs
          </TabsTrigger>
          <TabsTrigger value="sources" className="gap-1.5 text-xs">
            <Link2 className="h-3.5 w-3.5" />
            Sources
          </TabsTrigger>
          <TabsTrigger value="changes" className="gap-1.5 text-xs">
            <GitCompareArrows className="h-3.5 w-3.5" />
            Changes
          </TabsTrigger>
        </TabsList>

        {/* 内容区：可滚动 */}
        <div className="flex-1 overflow-hidden">
          {/* Outputs Tab：使用独立子组件 */}
          <TabsContent value="outputs" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-3">
                <OutputsTab items={MOCK_OUTPUTS} />

                <Separator className="my-3" />

                <ConversationTraceBlock onOpenLogs={onOpenLogs} />

                <Separator className="my-3" />
              </div>
            </ScrollArea>
          </TabsContent>

          {/* Sources Tab：使用独立子组件 */}
          <TabsContent value="sources" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-3">
                <SourcesTab items={MOCK_SOURCES} />

                <Separator className="my-3" />

                {/* 预留扩展区块：使用独立子组件 */}
                <ContextBlock />
                <McpBlock />
                <SubagentBlock />
              </div>
            </ScrollArea>
          </TabsContent>

          {/* Changes Tab：task 级文件变更集 */}
          <TabsContent value="changes" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <ChangesTab taskId={activeTaskId} />
            </ScrollArea>
          </TabsContent>
        </div>
      </Tabs>
    </aside>
  );
}
