import { ArrowUpRightIcon, MoreHorizontalIcon } from "lucide-react";
import { DropdownMenu } from "radix-ui";

import type { LinkOpener } from "@/components/links/use-open-link";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { MiddleTruncate } from "@/components/ui/middle-truncate";
import type { TransportDeliverable } from "@/lib/assistant/contract";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { CAPSULE_HEIGHT } from "@/lib/assistant/turn/deliverable-layout";
import { FILE_EXITS } from "@/lib/desktop-host/file-exits";
import { formatByteSize } from "@/lib/format/byte-size";
import { cn } from "@/lib/utils";
import { DeliverableBadge, describeDeliverable } from "@/components/deliverables/deliverable-label";
import { unavailableReason, useDeliverableFile } from "@/components/deliverables/use-deliverable-file";

/** 高度取 `CAPSULE_HEIGHT`（与估高同一个数），不写在类名里。 */
const CAPSULE_CLASS = "group/capsule bg-card relative inline-flex min-w-0 max-w-full items-center rounded-lg border shadow-xs hover:bg-muted focus-within:bg-muted";
/** 已不存在、无法访问：虚线边框、没有投影与底色，悬停也不变灰（不可点）。 */
const UNAVAILABLE_CLASS = "border-dashed bg-transparent shadow-none hover:bg-transparent";
const FOCUS_CLASS = "outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset";

/**
 * 一枚交付物胶囊：容器里并列主按钮（角标、名称、大小）与次按钮（出口菜单），不嵌套按钮。
 * 工作区内的文件现读信息：大小随文件变化更新；文件被删、无法访问时虚线边框、名称划线、不可点（不用警示色，
 * 文件被移走是正常事）。目录、网址与工作区外的条目沿用交付时的记录。
 * 主按钮的去向只来自打开器（`resolveDeliverable`）；打不开的条目（浏览器开发模式下的工作区外文件）不可点。
 * 次按钮是把文件交给系统的菜单（`FILE_EXITS`：快速查看、用系统应用打开、在文件夹中显示），给文件与目录，工作区外的
 * 也给（主操作是「在文件夹中显示」，菜单多出另外两项）；网址没有，没有桌面宿主时也没有。次按钮悬停、取得焦点或菜单
 * 开着时出现，叠在大小那一格上，胶囊宽度不变；Tab 先到主按钮再到次按钮。
 */
export function DeliverableCapsule({ deliverable, links }: { deliverable: TransportDeliverable; links: LinkOpener }) {
  // 大小先取交付时的记录，现读到了再换（`useDeliverableFile`）。
  const recorded = describeDeliverable(deliverable);
  const { name, path, badge } = recorded;
  const file = useDeliverableFile(deliverable);
  const reason = unavailableReason(file);
  const unavailable = reason !== null;
  // 大小一格：打不开写原因，其余读取故障写「—」。
  const meta = reason ?? (file.problem ? "—" : file.info ? formatByteSize(file.info.size) : recorded.meta);
  const target = unavailable ? null : links.resolveDeliverable(deliverable);
  const exits = !unavailable && deliverable.type !== "url" ? FILE_EXITS : [];
  const revealable = exits.length > 0;
  return (
    <div data-slot="deliverable-capsule" data-unavailable={unavailable || undefined} style={{ height: CAPSULE_HEIGHT }} className={cn(CAPSULE_CLASS, unavailable && UNAVAILABLE_CLASS)}>
      <Tooltip>
        <TooltipTrigger
          render={
            <button
              type="button"
              // 打不开时用 aria-disabled 而不是 disabled：仍能用 Tab 聚焦、读到提示条里的路径与原因，点击无效。
              aria-disabled={!target || undefined}
              aria-label={`${target ? (links.describe(target) ?? "打开") : "无法打开"} · ${name}`}
              onClick={() => target && links.open(target)}
              className={cn("flex h-full min-w-0 items-center gap-2 rounded-[inherit] [corner-shape:inherit] pr-3 pl-2 text-left aria-disabled:cursor-default", FOCUS_CLASS)}
            />
          }
        >
          <DeliverableBadge>{badge}</DeliverableBadge>
          <MiddleTruncate text={name} className={cn("max-w-[260px] text-sm", unavailable && "text-muted-foreground line-through")} />
          <span className={cn("text-muted-foreground inline-flex min-w-7 shrink-0 items-center gap-0.5 text-xs tabular-nums", revealable && "group-hover/capsule:invisible group-focus-within/capsule:invisible group-has-[[data-state=open]]/capsule:invisible")}>
            {meta}
            {deliverable.type === "url" && <ArrowUpRightIcon className="size-3" aria-hidden="true" />}
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-96">
          <span className="flex flex-col gap-0.5 text-left">
            {deliverable.caption && <span>{deliverable.caption}</span>}
            <span className={cn(deliverable.caption && "opacity-70")}>{path}</span>
          </span>
        </TooltipContent>
      </Tooltip>
      {revealable && deliverable.type !== "url" && (
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <button
              type="button"
              aria-label={`更多操作 ${name}`}
              title="更多操作"
              className={cn("text-muted-foreground hover:text-foreground hover:bg-foreground/8 data-[state=open]:bg-foreground/8 invisible absolute right-1 grid size-7 place-items-center rounded-md group-hover/capsule:visible group-focus-within/capsule:visible data-[state=open]:visible", FOCUS_CLASS)}
            >
              <MoreHorizontalIcon className="size-3.5" />
            </button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content align="end" sideOffset={4} className={MENU_CONTENT_CLASS}>
              {exits.map((exit) => (
                <DropdownMenu.Item key={exit.id} className={MENU_ITEM_CLASS} onSelect={() => exit.run(deliverable.absolutePath)}>{exit.label}</DropdownMenu.Item>
              ))}
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      )}
    </div>
  );
}
