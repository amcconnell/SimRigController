interface MuteButtonProps {
  muted: boolean;
  onToggle: () => void;
}

export function MuteButton({ muted, onToggle }: MuteButtonProps) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={muted}
      aria-label={muted ? "Unmute audio" : "Mute audio"}
      title={muted ? "Audio is muted — click to unmute" : "Mute all output"}
      className={`flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-medium uppercase tracking-wide transition-colors ${
        muted
          ? "border-rose-500/50 bg-rose-500/20 text-rose-200 hover:bg-rose-500/30"
          : "border-zinc-700 bg-zinc-800 text-zinc-300 hover:border-zinc-500 hover:bg-zinc-700"
      }`}
    >
      <span aria-hidden className="text-sm leading-none">{muted ? "🔇" : "🔊"}</span>
      <span>{muted ? "Muted" : "Mute"}</span>
    </button>
  );
}
