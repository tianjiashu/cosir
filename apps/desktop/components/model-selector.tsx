"use client";

import {
  useCallback,
  useMemo,
  useRef,
  useState,
  createContext,
  useContext,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { CheckIcon, ChevronDownIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { REASONING_EFFORT_VALUES } from "@/lib/model-selection-constants";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { RadioGroup as RadioGroupPrimitive } from "radix-ui";

export type ModelSelectorEffortOption = {
  id: string;
  name: string;
};

/** 推理强度的展示名；取值仍是 REASONING_EFFORT_VALUES，系统配置的推理强度下拉也复用这份标签。 */
const EFFORT_OPTION_LABELS: Record<(typeof REASONING_EFFORT_VALUES)[number], string> = { low: "低", high: "高", max: "最高" };

export const DEFAULT_EFFORT_OPTIONS: readonly ModelSelectorEffortOption[] = REASONING_EFFORT_VALUES.map(
  (id) => ({ id, name: EFFORT_OPTION_LABELS[id] }),
);

export type ModelOption = {
  id: string;
  name: string;
  triggerName?: string;
  description?: string;
  icon?: ReactNode;
  disabled?: boolean;
  /** Extra terms matched by ModelSelector.Search, in addition to id and name. */
  keywords?: readonly string[];
  /**
   * Reasoning effort levels the model supports. Pass `true` for the default
   * low/high/max levels, or a custom list. Omit for models without
   * configurable reasoning.
   */
  efforts?: boolean | readonly ModelSelectorEffortOption[];
};

function getModelEfforts(
  model: ModelOption | undefined,
): readonly ModelSelectorEffortOption[] | undefined {
  if (!model?.efforts) return undefined;
  return model.efforts === true ? DEFAULT_EFFORT_OPTIONS : model.efforts;
}

function resolveEffort(
  efforts: readonly ModelSelectorEffortOption[] | undefined,
  effort: string | undefined,
): string | undefined {
  if (effort === undefined) return undefined;
  return efforts?.some((e) => e.id === effort) ? effort : undefined;
}

type ModelSelectorContextValue = {
  models: readonly ModelOption[];
  value: string;
  setValue: (value: string) => void;
  /** The model matching `value`, derived once for all sub-components. */
  selectedModel: ModelOption | undefined;
  /** The selected model's effort levels, undefined when not configurable. */
  efforts: readonly ModelSelectorEffortOption[] | undefined;
  /** Effort resolved against the selected model's supported levels. */
  effort: string | undefined;
  setEffort: (effort: string) => void;
  setOpen: (open: boolean) => void;
};

const ModelSelectorContext = createContext<ModelSelectorContextValue | null>(
  null,
);

function useModelSelectorContext() {
  const ctx = useContext(ModelSelectorContext);
  if (!ctx) {
    throw new Error(
      "ModelSelector sub-components must be used within ModelSelector.Root",
    );
  }
  return ctx;
}

/**
 * The selected model's effort levels and the active selection. Use it to build
 * a custom effort UI inside ModelSelector.Content (e.g. a slider or a shadcn
 * DropdownMenu) when the built-in ModelSelector.Effort layout doesn't fit.
 * `efforts` is undefined for models without configurable reasoning.
 */
function useModelSelectorEfforts(): {
  efforts: readonly ModelSelectorEffortOption[] | undefined;
  effort: string | undefined;
  setEffort: (effort: string) => void;
} {
  const { efforts, effort, setEffort } = useModelSelectorContext();
  return { efforts, effort, setEffort };
}

type ModelSelectorRootProps = {
  models: readonly ModelOption[];
  /** 选择只受控：所选模型的事实在调用方（偏好存储 / 表单），组件不另存一份。 */
  value: string;
  onValueChange: (value: string) => void;
  effort?: string;
  onEffortChange?: (effort: string) => void;
  children: ReactNode;
};

function ModelSelectorRoot({
  models,
  value,
  onValueChange,
  effort,
  onEffortChange,
  children,
}: ModelSelectorRootProps) {
  const [open, setOpen] = useState(false);
  const setEffort = useCallback((next: string) => onEffortChange?.(next), [onEffortChange]);

  const selectedModel = models.find((m) => m.id === value);
  const efforts = getModelEfforts(selectedModel);
  const activeEffort = resolveEffort(efforts, effort);
  const contextValue = useMemo(
    () => ({
      models,
      value,
      setValue: onValueChange,
      selectedModel,
      efforts,
      effort: activeEffort,
      setEffort,
      setOpen,
    }),
    [
      models,
      value,
      onValueChange,
      selectedModel,
      efforts,
      activeEffort,
      setEffort,
      setOpen,
    ],
  );

  return (
    <ModelSelectorContext.Provider value={contextValue}>
      <Popover open={open} onOpenChange={setOpen}>
        {children}
      </Popover>
    </ModelSelectorContext.Provider>
  );
}

const modelSelectorTriggerVariants = cva(
  "focus-visible:ring-ring/50 flex w-fit items-center justify-between gap-2 overflow-hidden rounded-md text-sm whitespace-nowrap transition-colors outline-none focus-visible:ring-1 disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        outline:
          "border-input hover:bg-accent hover:text-accent-foreground border bg-transparent",
        ghost: "hover:bg-accent hover:text-accent-foreground",
        muted: "bg-secondary text-secondary-foreground hover:bg-secondary/80",
      },
      size: {
        default: "h-9 px-3 py-2",
        sm: "h-8 px-2.5 py-1.5 text-xs",
        lg: "h-10 px-4 py-2.5",
      },
    },
    defaultVariants: {
      variant: "outline",
      size: "default",
    },
  },
);

type ModelSelectorTriggerProps = ComponentPropsWithoutRef<
  typeof PopoverTrigger
> &
  VariantProps<typeof modelSelectorTriggerVariants>;

function ModelSelectorTrigger({
  className,
  variant,
  size,
  children,
  onKeyDown,
  ...props
}: ModelSelectorTriggerProps) {
  const { setOpen } = useModelSelectorContext();

  return (
    <PopoverTrigger
      data-slot="model-selector-trigger"
      data-variant={variant ?? "outline"}
      data-size={size ?? "default"}
      role="combobox"
      aria-haspopup="listbox"
      className={cn(modelSelectorTriggerVariants({ variant, size }), className)}
      onKeyDown={(e) => {
        onKeyDown?.(e);
        if (e.defaultPrevented) return;
        // ARIA combobox: arrows open the listbox from a focused trigger.
        // Popover leaves this to the consumer.
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          setOpen(true);
        }
      }}
      {...props}
    >
      {children ?? <ModelSelectorValue />}
      <ChevronDownIcon className="size-4 opacity-50" />
    </PopoverTrigger>
  );
}

type ModelSelectorValueProps = {
  placeholder?: ReactNode;
  /** Show the active effort level next to the model name. */
  showEffort?: boolean;
  className?: string;
};

function ModelIcon({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "flex size-3.5 shrink-0 items-center justify-center [&_svg]:size-3.5",
        className,
      )}
    >
      {children}
    </span>
  );
}

function ModelSelectorValue({
  placeholder = "选择模型",
  showEffort = true,
  className,
}: ModelSelectorValueProps) {
  const { selectedModel, efforts, effort } = useModelSelectorContext();

  if (!selectedModel) {
    return (
      <span
        data-slot="model-selector-value"
        className={cn("text-muted-foreground", className)}
      >
        {placeholder}
      </span>
    );
  }

  const effortName =
    showEffort && effort !== undefined
      ? efforts?.find((e) => e.id === effort)?.name
      : undefined;

  return (
    <span
      data-slot="model-selector-value"
      className={cn("flex min-w-0 items-center gap-2", className)}
    >
      {selectedModel.icon && <ModelIcon>{selectedModel.icon}</ModelIcon>}
      <span className="truncate font-medium">{selectedModel.triggerName ?? selectedModel.name}</span>
      {/* 选项标签与系统配置下拉共用，那里有字段名作上下文；触发器上单独一个「低」看不出含义，故加前缀。 */}
      {effortName && (
        <span className="text-muted-foreground shrink-0 whitespace-nowrap" title={`推理强度：${effortName}`}>
          推理 {effortName}
        </span>
      )}
    </span>
  );
}

type ModelSelectorContentProps = Omit<
  ComponentPropsWithoutRef<typeof PopoverContent>,
  "side"
> & {
  /**
   * Preferred side for the initial placement. Once the popover is open, the
   * rendered side takes over until it closes, so the popup does not jump
   * between sides while filtering resizes the list.
   */
  side?: ComponentPropsWithoutRef<typeof PopoverContent>["side"];
  searchable?: boolean;
};

// The popover re-evaluates collision flipping whenever the popup resizes, so
// filtering the list down flips the popup back to the preferred side
// mid-interaction. Feed the rendered side back as the preferred side, making
// the popup keep its side until it no longer fits.
function useLazyFlipSide(): {
  side: ModelSelectorContentProps["side"];
  popupRef: (node: HTMLDivElement | null) => void;
} {
  const [side, setSide] = useState<ModelSelectorContentProps["side"]>();
  const observerRef = useRef<MutationObserver | null>(null);
  const popupRef = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node) {
      setSide(undefined);
      return;
    }
    const sync = () => {
      const rendered = node.getAttribute("data-side");
      if (rendered) setSide(rendered as ModelSelectorContentProps["side"]);
    };
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(node, {
      attributes: true,
      attributeFilter: ["data-side"],
    });
    observerRef.current = observer;
  }, []);
  return { side, popupRef };
}

/**
 * Hidden input that anchors cmdk's keyboard navigation, keeping the list
 * keyboard-operable without a visible search box. ModelSelectorContent renders
 * one automatically when unfiltered.
 */
function ModelSelectorFocusAnchor() {
  return (
    <div className="sr-only">
      <CommandInput readOnly aria-label="模型" />
    </div>
  );
}

function ModelSelectorContent({
  className,
  align = "start",
  side,
  sideOffset = 6,
  searchable,
  children,
  ...props
}: ModelSelectorContentProps) {
  const { value } = useModelSelectorContext();
  const { side: renderedSide, popupRef } = useLazyFlipSide();
  const unfiltered =
    searchable === false || (!searchable && children === undefined);

  return (
    <PopoverContent
      ref={popupRef}
      data-slot="model-selector-content"
      align={align}
      side={renderedSide ?? side ?? "bottom"}
      sideOffset={sideOffset}
      className={cn(
        "bg-popover w-72 min-w-(--radix-popover-trigger-width) overflow-hidden rounded-xl p-0",
        className,
      )}
      {...props}
    >
      <Command
        className="bg-transparent"
        shouldFilter={!unfiltered}
        {...(value !== undefined ? { defaultValue: value } : {})}
      >
        {unfiltered && <ModelSelectorFocusAnchor />}
        {children ?? (
          <>
            {searchable && <ModelSelectorSearch />}
            <ModelSelectorList />
            <ModelSelectorEffort />
          </>
        )}
      </Command>
    </PopoverContent>
  );
}

type ModelSelectorSearchProps = ComponentPropsWithoutRef<
  typeof CommandInput
>;

function ModelSelectorSearch({
  placeholder = "搜索模型…",
  ...props
}: ModelSelectorSearchProps) {
  return (
    <CommandInput
      data-slot="model-selector-search"
      placeholder={placeholder}
      {...props}
    />
  );
}

type ModelSelectorListProps = ComponentPropsWithoutRef<
  typeof CommandList
>;

function ModelSelectorList({
  className,
  children,
  ...props
}: ModelSelectorListProps) {
  const { models } = useModelSelectorContext();

  return (
    <CommandList
      data-slot="model-selector-list"
      className={cn(
        "[-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        className,
      )}
      {...props}
    >
      {children ?? (
        <>
          <ModelSelectorEmpty />
          <CommandGroup>
            {models.map((model) => (
              <ModelSelectorItem key={model.id} model={model} />
            ))}
          </CommandGroup>
        </>
      )}
    </CommandList>
  );
}

type ModelSelectorEmptyProps = ComponentPropsWithoutRef<
  typeof CommandEmpty
>;

function ModelSelectorEmpty({ children, ...props }: ModelSelectorEmptyProps) {
  return (
    <CommandEmpty data-slot="model-selector-empty" {...props}>
      {children ?? "没有匹配的模型"}
    </CommandEmpty>
  );
}

function ModelSelectorItem({ model }: { model: ModelOption }) {
  const { value, setValue, setOpen } = useModelSelectorContext();
  const isSelected = value === model.id;

  return (
    <CommandItem
      data-slot="model-selector-item"
      value={model.id}
      keywords={[model.name, ...(model.keywords ?? [])]}
      {...(model.disabled ? { disabled: true } : undefined)}
      onSelect={() => {
        setValue(model.id);
        setOpen(false);
      }}
      className="relative items-start gap-2 rounded-lg py-2 ps-3 pe-9 [&_svg:not([class*='size-'])]:size-3.5"
    >
      {model.icon && (
        <ModelIcon className="mt-[3px]">{model.icon}</ModelIcon>
      )}
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-medium">{model.name}</span>
        {model.description && (
          <span className="text-muted-foreground truncate text-xs">
            {model.description}
          </span>
        )}
      </span>
      {isSelected && (
        <span className="absolute end-3 top-2.5 flex size-4 items-center justify-center">
          <CheckIcon className="size-4" />
        </span>
      )}
    </CommandItem>
  );
}

type ModelSelectorEffortProps = ComponentPropsWithoutRef<"div"> & {
  label?: ReactNode;
};

function ModelSelectorEffort({
  label = "思考",
  className,
  onKeyDown,
  ...props
}: ModelSelectorEffortProps) {
  const { efforts, effort, setEffort } = useModelSelectorEfforts();

  if (!efforts?.length) return null;

  return (
    <div
      data-slot="model-selector-effort"
      className={cn(
        "flex cursor-default items-center justify-between gap-3 border-t px-3 py-2",
        className,
      )}
      onKeyDown={(e) => {
        onKeyDown?.(e);
        if (e.defaultPrevented) return;
        // cmdk's Command root claims Home/End to jump the model list; stop
        // them here so only the radiogroup reacts.
        if (e.key === "Home" || e.key === "End") e.stopPropagation();
        // Vertical arrows refocus cmdk's input before the event bubbles to
        // the Command root: the same keypress then moves the list highlight,
        // and Enter selects again (cmdk's Enter is inert while a radio has
        // focus, so the highlight would otherwise move with no way to act).
        if (e.key === "ArrowUp" || e.key === "ArrowDown") {
          e.currentTarget
            .closest("[cmdk-root]")
            ?.querySelector<HTMLInputElement>("[cmdk-input]")
            ?.focus();
        }
      }}
      {...props}
    >
      <span className="text-muted-foreground text-xs">{label}</span>
      <RadioGroupPrimitive.Root
        value={effort ?? ""}
        onValueChange={setEffort}
        orientation="horizontal"
        aria-label={typeof label === "string" ? label : "推理强度"}
        className="flex items-center gap-0.5"
      >
        {efforts.map((option) => (
          <RadioGroupPrimitive.Item
            key={option.id}
            value={option.id}
            className={cn(
              "focus-visible:ring-ring/50 text-muted-foreground hover:bg-muted hover:text-foreground cursor-pointer rounded-md px-2 py-1 text-xs transition-colors outline-none focus-visible:ring-1",
              "data-[state=checked]:bg-accent data-[state=checked]:text-accent-foreground data-[state=checked]:font-medium",
            )}
          >
            {option.name}
          </RadioGroupPrimitive.Item>
        ))}
      </RadioGroupPrimitive.Root>
    </div>
  );
}

export {
  ModelSelectorRoot,
  ModelSelectorTrigger,
  ModelSelectorValue,
  ModelSelectorContent,
  ModelSelectorSearch,
  ModelSelectorList,
  ModelSelectorEffort,
};
