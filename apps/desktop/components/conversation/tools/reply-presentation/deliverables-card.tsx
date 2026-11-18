import { useOpenLink } from "@/components/links/use-open-link";
import { deliverableGroups, type DeliverablesBatch } from "@/lib/assistant/turn/deliverable-layout";
import { DeliverableCapsule } from "./deliverable-capsule";
import { DeliverableImageCard, DeliverableTileRow } from "./deliverable-tile-row";

/**
 * 一次交付的卡：顶部是 Agent 写的说明（有才显示，纯文本、不截断——上限在参数模型，最多三行），下面是成果；没有外层
 * 边框，作为回合里的一个块放在调用的位置、过程收起时紧贴最终回复上方（排位在 `turn-layout`）。工作区内的图片排成一行
 * 方块（只有一张时放大成大图卡），其余是胶囊、另起一行（分组规则在 `deliverable-layout`）。哪些东西算交付物只读 part
 * 上的交付记录，不从正文链接或文件操作里推断。继承来的历史同样可打开。
 */
export function DeliverablesCard({ card, sessionId }: { card: DeliverablesBatch; sessionId: number }) {
  const links = useOpenLink("tool", sessionId);
  const { tiles, capsules, card: imageCard } = deliverableGroups(card);
  return (
    <section data-slot="deliverables-card" aria-label={card.note ?? "交付物"} className="flex flex-col gap-2">
      {card.note && <p data-slot="deliverables-note" className="text-foreground text-sm leading-6 wrap-anywhere whitespace-pre-line">{card.note}</p>}
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
