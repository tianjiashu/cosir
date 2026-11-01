/**
 * Cosir 品牌标志，与应用图标同一几何（`electron/resources/icons/source/cosir-icon.svg`），改动须两处同步。
 * 底色与字形色是品牌固定色，不随主题变化。各部件带 `cosir-mark__*` 类名，只供使用方编排动画，组件本身是静态的。
 */
export function CosirMark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 1024 1024" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
      <rect className="cosir-mark__plate" width="1024" height="1024" rx="228" fill="#121715" />
      <g fill="#B6FF3B">
        <path className="cosir-mark__c" d="M348 232H752V348H396L328 416V608L396 676H752V792H348L208 652V372Z" />
        <path className="cosir-mark__prompt" d="M456 400L608 512L456 624L412 564L484 512L412 460Z" />
        <rect className="cosir-mark__cursor" x="632" y="556" width="184" height="68" />
      </g>
    </svg>
  );
}
