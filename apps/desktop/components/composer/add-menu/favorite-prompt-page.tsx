import { useSyncExternalStore } from "react";
import { BookOpenTextIcon } from "lucide-react";

import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { EmptyState } from "@/components/ui/state-panel";
import { insertText, useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { readFavoritePrompts, subscribeFavoritePrompts } from "@/lib/favorite-prompt-storage";
import { AddMenuPageHeader } from "./add-menu-page-header";

/**
 * 「+」菜单的常用提示词页：选中后把提示词正文插入光标处；「管理」打开提示词库对话框
 * （`FavoritePromptManager`，由菜单持有，菜单收起后对话框仍在）。提示词库是本机偏好存储，订阅它的变化。
 */
export function FavoritePromptPage({ onBack, onPicked, onManage }: {
  onBack: () => void;
  onPicked: () => void;
  onManage: () => void;
}) {
  const insertion = useComposerInsertion();
  const prompts = useSyncExternalStore(subscribeFavoritePrompts, readFavoritePrompts, readFavoritePrompts);

  return (
    <>
      <AddMenuPageHeader
        title="常用提示词"
        onBack={onBack}
        trailing={(
          <button type="button" onClick={onManage} className="text-muted-foreground hover:text-foreground rounded px-1.5 py-1 text-xs focus-ring">
            管理
          </button>
        )}
      />
      {prompts.length === 0 ? (
        <EmptyState
          icon={<BookOpenTextIcon className="size-5" />}
          title="还没有常用提示词，点击“管理”添加。"
          className="border-0 px-3 py-6"
        />
      ) : (
        // value 用 id 保证同名提示词各自可选；筛选只看名称与正文，不让 id 里的字符参与匹配。
        <Command filter={(_value, search, keywords) => (keywords ?? []).some((text) => text.toLowerCase().includes(search.toLowerCase())) ? 1 : 0}>
          <CommandInput placeholder="搜索提示词" aria-label="搜索提示词" autoFocus />
          <CommandList aria-label="常用提示词" className="max-h-64">
            <CommandEmpty className="text-muted-foreground py-4 text-xs">没有匹配的提示词</CommandEmpty>
            {prompts.map((prompt) => (
              <CommandItem
                key={prompt.id}
                value={prompt.id}
                keywords={[prompt.title, prompt.content]}
                onSelect={() => {
                  insertText(insertion.dispatch, prompt.content);
                  onPicked();
                }}
                className="flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 [&>svg:last-child]:hidden"
              >
                <span className="text-sm font-medium">{prompt.title}</span>
                <span className="text-muted-foreground line-clamp-2 text-xs">{prompt.content}</span>
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      )}
    </>
  );
}
