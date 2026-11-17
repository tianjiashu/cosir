import { useOpenLink } from "@/components/links/use-open-link";
import { deliverableGroups, type DeliverablesCard as DeliverablesCardData } from "@/lib/assistant/turn/deliverable-layout";
import { DeliverableCapsule } from "./deliverable-capsule";
import { DeliverableImageCard, DeliverableTileRow } from "./deliverable-tile-row";

/**
 * 回合的交付物组：一个回合里全部 `present_deliverables` 调用合成的一组，没有外层卡片，直接跟在正文下面。
 * 工作区内的图片排成一行方块（只有一张时放大成大图卡），其余是胶囊、另起一行（分组规则在 `deliverable-layout`）。
 * 哪些东西算交付物只读 part 上的交付记录，不从正文链接或文件操作里推断。继承来的历史同样可打开。
 */
export function DeliverablesCard({ card, sessionId }: { card: DeliverablesCardData; sessionId: number }) {
  const links = useOpenLink("tool", sessionId);
  const { tiles, capsules, card: imageCard } = deliverableGroups(card);
  return (
    <section data-slot="deliverables-card" aria-label="交付物" className="flex flex-col gap-2">
      {imageCard && <DeliverableImageCard card={imageCard} />}
      {tiles.length > 0 && <DeliverableTileRow entries={tiles} />}
      {capsules.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {capsules.map(({ deliverable }) => <DeliverableCapsule key={deliverable.key} deliverable={deliverable} links={links} />)}
        </div>
      )}
    </section>
  );
}
