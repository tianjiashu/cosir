/**
 * 鍙充晶淇℃伅闈㈡澘锛圧ightPanel锛夈€?
 *
 * 缁勫悎浠ヤ笅瀛愮粍浠讹細
 * - OutputsTab锛歄utputs 鍒楄〃锛堜换鍔′骇鐢?淇敼鐨勬枃浠躲€佹枃妗ｏ級
 * - SourcesTab锛歋ources 鍒楄〃锛堝紩鐢ㄦ枃妗ｃ€佷笂涓嬫枃鐗囨銆佽鍒欐枃浠讹級
 * - ContextBlock锛氫笂涓嬫枃寮曠敤鍗犱綅
 * - ConversationTraceBlock锛氬綋鍓嶅璇?trace 璇婃柇鍏ュ彛
 * - McpBlock锛歁CP 鍏ュ彛鍗犱綅
 * - SubagentBlock锛歋ubagent 鍒嗙粍鍗犱綅
 *
 * 绗竴鐗堜娇鐢ㄩ潤鎬?mock 鏁版嵁銆?
 *
 * @module components/layout/RightPanel
 */

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { OutputsTab, type OutputItem } from "@/components/right-panel/OutputsTab";
import { SourcesTab, type SourceItem } from "@/components/right-panel/SourcesTab";
import { ContextBlock } from "@/components/right-panel/ContextBlock";
import { ConversationTraceBlock } from "@/components/right-panel/ConversationTraceBlock";
import { McpBlock } from "@/components/right-panel/McpBlock";
import { SubagentBlock } from "@/components/right-panel/SubagentBlock";

/** Mock Outputs 鏁版嵁锛堢涓€鐗堥潤鎬佹暟鎹級銆?*/
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

/** Mock Sources 鏁版嵁锛堢涓€鐗堥潤鎬佹暟鎹級銆?*/
const MOCK_SOURCES: SourceItem[] = [
  {
    id: "src-1",
    name: "docs/desktop-client-development-plan.md",
    description: "妗岄潰瀹㈡埛绔紑鍙戣鏍间笌瀹炴柦娓呭崟",
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
 * RightPanel 缁勪欢灞炴€с€?
 */
interface RightPanelProps {
  /** 鎵撳紑鏃ュ織椤甸潰銆?*/
  onOpenLogs: () => void;
}

/**
 * 鍙充晶淇℃伅闈㈡澘缁勪欢銆?
 *
 * 鍥哄畾瀹藉害 ~280px锛岄€氳繃 Tabs 鍒囨崲 Outputs/Sources锛?
 * 搴曢儴灞曠ず棰勭暀鎵╁睍鍖哄潡銆?
 *
 * @param props - 缁勪欢灞炴€с€?
 * @returns 鍙充晶淇℃伅闈㈡澘銆?
 */
export function RightPanel({ onOpenLogs }: RightPanelProps) {
  return (
    <aside className="flex h-full w-72 flex-col border-l border-border bg-background">
      <Tabs defaultValue="outputs" className="flex h-full flex-col">
        {/* Tab 鍒囨崲鏍?*/}
        <TabsList className="mx-2 mt-2 w-[calc(100%-1rem)]">
          <TabsTrigger value="outputs" className="gap-1.5 text-xs">
            馃搫 Outputs
          </TabsTrigger>
          <TabsTrigger value="sources" className="gap-1.5 text-xs">
            馃敆 Sources
          </TabsTrigger>
        </TabsList>

        {/* 鍐呭鍖猴細鍙粴鍔?*/}
        <div className="flex-1 overflow-hidden">
          {/* Outputs Tab 鈥?浣跨敤鐙珛瀛愮粍浠?*/}
          <TabsContent value="outputs" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-3">
                <OutputsTab items={MOCK_OUTPUTS} />

                <Separator className="my-3" />

                <ConversationTraceBlock onOpenLogs={onOpenLogs} />

                <Separator className="my-3" />

                {/* 棰勭暀鎵╁睍鍖哄潡 鈥?浣跨敤鐙珛瀛愮粍浠?*/}
              </div>
            </ScrollArea>
          </TabsContent>

          {/* Sources Tab 鈥?浣跨敤鐙珛瀛愮粍浠?*/}
          <TabsContent value="sources" className="mt-0 h-full">
            <ScrollArea className="h-full scrollbar-thin">
              <div className="space-y-1 p-3">
                <SourcesTab items={MOCK_SOURCES} />

                <Separator className="my-3" />

                {/* 棰勭暀鎵╁睍鍖哄潡 鈥?浣跨敤鐙珛瀛愮粍浠?*/}
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
