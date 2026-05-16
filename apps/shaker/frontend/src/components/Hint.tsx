interface HintProps {
  text: string;
}

/** Small "?" indicator with a hover-revealed tooltip. */
export function Hint({ text }: HintProps) {
  return (
    <span className="group relative inline-flex">
      <span
        className="flex h-4 w-4 shrink-0 cursor-help items-center justify-center rounded-full border border-zinc-700 text-[10px] leading-none text-zinc-500 transition-colors hover:border-zinc-500 hover:text-zinc-200"
        aria-label={text}
      >
        ?
      </span>
      <span
        role="tooltip"
        className="pointer-events-none invisible absolute bottom-full left-1/2 z-30 mb-1 w-60 -translate-x-1/2 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs leading-snug text-zinc-200 opacity-0 shadow-lg transition-opacity duration-100 group-hover:visible group-hover:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}
