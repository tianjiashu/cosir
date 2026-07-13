/**
 * 右侧信息面板（RightPanel）。
 *
 * 组合以下子组件：
 * - OutputsTab：Outputs 列表（任务产生/修改的文件、文档）
 * - SourcesTab：Sources 列表（引用文档、上下文片段、规则文件）
 * - CheckpointBlock：Checkpoint 区块占位
 * - ContextBlock：上下文引用占位
 * - McpBlock：MCP 入口占位
 * - SubagentBlock：Subagent 分组占位
 *
 * 第一版使用静态 mock 数据。
 *
 * @module components/layout/RightPanel
 */

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { OutputsTab, type OutputItem } from "@/components/right-panel/OutputsTab";
import { SourcesTab, type SourceItem } from "@/components/right-panel/SourcesTab";
import { CheckpointBlock } from "@/components/right-panel/CheckpointBlock";
import { ContextBlock } from "@/components/right-panel/ContextBlock";
import { McpBlock } from "@/components/right-panel/McpBlock";
import { SubagentBlock } from "@/components/right-panel/SubagentBlock";

/** Mock Outputs 数据（第一版静态数据）。 */
const MOCK_OUTPUTS: OutputItem[] = [
  {
    id: "out-1",
    name: "rules/Agent客户端代码开发规范.md",
    description: "客户端代码开发规范文档（新建）",
  },
  {
    id: "out-2",
    name: "AGENTS.md",
    description: "更新路由表，补充客户端规范入口",
  },
];

/** Mock Sources 数据（第一版静态数据）。 */
const MOCK_SOURCES: SourceItem[] = [
  {
    id: "src-1",
    name: "docs/desktop-client-development-plan.md",
    description: "桌面客户端开发规格与实施清单",
  },
  {
    id: "src-2",
    name: "rules/Agent代码开发规范.md",
    description: "后端代码开发规范（技术栈无关）",
  },
  {
    id: "src-3",
    name: "docs/ui-guidelines.md",
    description: "UI 视觉与组件实现指南",
  },
];

/**
 * 右侧信息面板组件。
 *
 * 固定宽度 ~280px，通过 Tabs 切换 Outputs/Sources，
 * 底部展示预留扩展区块。
 */
export function RightPanel() {
  return (
    <aside className="flex h-full w-72 flex-col border-l border-border bg-background">
      <Tabs defaultValue="outputs" className="flex h-full flex-col">
        {/* Tab 切换栏 */}
        <TabsList className="mx-2 mt-2 w-[calc(100%-1rem)]">
          <TabsTrigger value="outputs" className="gap-1.5 text-xs">
            📄 Outputs
          </TabsTrigger>
          <TabsTrigger value="sources" className="gap-1.5 text-xs">
            🔗 Sources
          </TabsTrigger>
        </TabsList>

        {/* 内容区：可滚动 */}
        <div className="flex-1 overflow-hidden">
          {/* Outputs Tab — 使用独立子组件 */}
          <TabsContent value="outputs" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-3">
                <OutputsTab items={MOCK_OUTPUTS} />

                <Separator className="my-3" />

                {/* 预留扩展区块 — 使用独立子组件 */}
                <CheckpointBlock />
              </div>
            </ScrollArea>
          </TabsContent>

          {/* Sources Tab — 使用独立子组件 */}
          <TabsContent value="sources" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-3">
                <SourcesTab items={MOCK_SOURCES} />

                <Separator className="my-3" />

                {/* 预留扩展区块 — 使用独立子组件 */}
                <ContextBlock />
                <McpBlock />
                <SubagentBlock />
              </div>
            </ScrollArea>
          </TabsContent>
        </div>
      </Tabs>
    </aside>
  );
}
