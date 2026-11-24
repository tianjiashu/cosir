import { Block, type BlockProps } from "streamdown";

import { MARKDOWN_BLOCK_ATTRIBUTE, registerMarkdownBlock } from "@/lib/markdown-block-sources";

/**
 * `MarkdownText` 的块组件：照常渲染 Streamdown 的块，外面包一层登记源文的容器（见 `markdown-block-sources`）。
 *
 * 容器是普通块级 div：不能用 `display: contents`，外层 `space-y-*` 的间距落在直接子元素上，contents 元素
 * 不产生盒子、间距会丢。ref 回调随源文变化重新登记，流式增长时始终是最新的块源文。
 */
export function MarkdownSourceBlock(props: BlockProps) {
  return (
    <div
      {...{ [MARKDOWN_BLOCK_ATTRIBUTE]: "" }}
      ref={(element) => {
        if (element) registerMarkdownBlock(element, props.content);
      }}
    >
      <Block {...props} />
    </div>
  );
}
