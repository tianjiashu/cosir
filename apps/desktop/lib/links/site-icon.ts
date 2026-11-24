import {
  siFigma,
  siGithub,
  siGitlab,
  siGoogle,
  siNotion,
  siNpm,
  siPypi,
  siStackoverflow,
  siWikipedia,
  siX,
  siYoutube,
} from "simple-icons";

/**
 * 高频站点的内置图标（按注册域匹配，子域名同样命中），跟随文字颜色。不从站点取 favicon：渲染链接不能联网，否则模型
 * 写出的主机名一显示就被请求，站点能看到本机 IP、子域名能夹带数据（宿主也拦下了界面的一切公网请求）；没收录的站点用通用图标。
 */
const BRAND_ICON_PATHS = new Map<string, string>([
  ["figma.com", siFigma.path],
  ["github.com", siGithub.path],
  ["githubusercontent.com", siGithub.path],
  ["gitlab.com", siGitlab.path],
  ["google.com", siGoogle.path],
  ["notion.so", siNotion.path],
  ["npmjs.com", siNpm.path],
  ["pypi.org", siPypi.path],
  ["stackoverflow.com", siStackoverflow.path],
  ["twitter.com", siX.path],
  ["wikipedia.org", siWikipedia.path],
  ["x.com", siX.path],
  ["youtu.be", siYoutube.path],
  ["youtube.com", siYoutube.path],
]);

/** 主机名对应的内置图标路径（24×24 viewBox）；没有收录时为 null。 */
export function brandIconPath(host: string): string | null {
  for (let domain = host; domain.includes("."); domain = domain.slice(domain.indexOf(".") + 1)) {
    const path = BRAND_ICON_PATHS.get(domain);
    if (path) return path;
  }
  return null;
}
