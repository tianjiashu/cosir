/** rehype-harden 仅把 /、./、../ 识别成相对地址；仓库文档常用裸文件名。 */
type HtmlNode = {
  type: string;
  properties?: Record<string, unknown>;
  children?: HtmlNode[];
};
export function normalizeWorkspaceUrls() {
  return (tree: HtmlNode) => {
    const visit = (node: HtmlNode) => {
      if (node.type === "element" && node.properties) {
        for (const key of ["href", "src"]) {
          const value = node.properties[key];
          if (
            typeof value === "string" &&
            value &&
            !/^(?:[a-z][a-z\d+.-]*:|[/#.]|\s)/i.test(value)
          ) {
            node.properties[key] = `./${value}`;
          }
        }
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}
