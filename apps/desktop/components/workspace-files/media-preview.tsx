import { useEffect, useRef, useState } from "react";

import { LoadingState } from "@/components/ui/state-panel";
import type { FileInfo } from "@/lib/api/workspace-files";
import { useFileResourceSrc, useResourceLoadFailure } from "@/lib/workspace-files/resources";
import { AddToChatButton } from "./add-to-chat-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";

/** 播放失败的原因，按 `MediaError.code` 分：首帧之前失败多半是格式或编码不支持，播放途中失败多半是文件损坏。 */
function failureReason(code: number | undefined, started: boolean): string {
  if (started) return "播放中断，文件可能已损坏";
  return code === MediaError.MEDIA_ERR_DECODE || code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED
    ? "此格式或编码无法在应用内播放"
    : "无法播放这个文件";
}

/**
 * 音频、视频：原生播放器直接用资源地址（后端支持分段读取，拖动进度按需取数据，不整文件读入内存）。
 *
 * 能不能播与机器有关（macOS 能硬件解 HEVC，AVI、ALAC 各平台都不行），所以不用 `canPlayType` 预判，先试着播、
 * 失败再按错误码上报 `onUnviewable`。先经 `useResourceLoadFailure` 排除不是内容问题的失败：媒体元素对 404、409 与
 * 后端暂时不在也只报格式错误，版本过期时地址自然换新，后端不在时不判失败。
 *
 * `.webm` 既可能是视频也可能只有声音，后缀分不出：一律用 `<video>` 播，读到元数据后没有画面就换成音频的版式，
 * 不留一块黑底。
 */
export function MediaPreview({
  workspaceId,
  info,
  navigation,
  onUnviewable,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
  onUnviewable: (reason: string) => void;
}) {
  const src = useFileResourceSrc(workspaceId, info.path, info.version);
  const failure = useResourceLoadFailure(workspaceId, info.path, src);
  const lastError = useRef<{ code: number | undefined; started: boolean }>({ code: undefined, started: false });
  const started = useRef(false);
  const [soundOnly, setSoundOnly] = useState(false);
  useEffect(() => {
    if (failure.failed) onUnviewable(failureReason(lastError.current.code, lastError.current.started));
  }, [failure.failed, onUnviewable]);
  const events = {
    src,
    controls: true,
    preload: "metadata" as const,
    onLoadedData: () => {
      started.current = true;
    },
    onLoadedMetadata: (event: React.SyntheticEvent<HTMLMediaElement>) => {
      if (event.currentTarget instanceof HTMLVideoElement && event.currentTarget.videoWidth === 0) setSoundOnly(true);
    },
    onError: (event: React.SyntheticEvent<HTMLMediaElement>) => {
      lastError.current = { code: event.currentTarget.error?.code, started: started.current };
      void failure.onError();
    },
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ReadingToolbar navigation={navigation} info={info} actions={<AddToChatButton workspaceId={workspaceId} path={info.path} />} />
      <div data-slot="media-preview" className={info.kind === "video" && !soundOnly ? "flex min-h-0 flex-1 items-center justify-center bg-black" : "flex flex-1 items-start justify-center p-6"}>
        {!src ? (
          <LoadingState className="m-auto" label="正在准备播放…" />
        ) : info.kind === "video" ? (
          <video {...events} aria-label={info.name} className={soundOnly ? "h-12 w-full max-w-xl" : "h-full w-full object-contain"} />
        ) : (
          <audio {...events} aria-label={info.name} className="w-full max-w-xl" />
        )}
      </div>
    </div>
  );
}
