interface BadgeProps {
  status: "WS LIVE" | "REST" | "LIVE" | "STALE" | "CONNECTING" | "DISCONNECTED";
  label?: string;
}

const STYLES: Record<string, string> = {
  "WS LIVE": "bg-teal-500/10 text-teal-400 border-teal-500/20",
  LIVE: "bg-teal-500/10 text-teal-400 border-teal-500/20",
  REST: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  STALE: "bg-rose-500/10 text-rose-400 border-rose-500/20",
  CONNECTING: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  DISCONNECTED: "bg-slate-500/10 text-slate-500 border-slate-500/20",
};

export function Badge({ status, label }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-[10px] font-medium border ${STYLES[status] || STYLES.DISCONNECTED}`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${
          status === "WS LIVE" || status === "LIVE" ? "bg-teal-400 animate-pulse" :
          status === "REST" ? "bg-amber-400 animate-pulse" :
          status === "STALE" ? "bg-rose-400" :
          status === "CONNECTING" ? "bg-amber-400 animate-pulse" :
          "bg-slate-500"
        }`}
      />
      {label || status}
    </span>
  );
}
