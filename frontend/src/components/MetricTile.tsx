interface MetricTileProps {
  label: string;
  value: string | number;
  color?: string;
  sub?: string;
}

export function MetricTile({ label, value, color, sub }: MetricTileProps) {
  return (
    <div className="bg-[#0a1628] p-3 rounded-xl border border-blue-500/[0.08] shadow-[0_0_20px_rgba(59,130,246,0.04)]">
      <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1 font-medium">
        {label}
      </div>
      <div className={`text-sm font-mono font-semibold ${color || "text-slate-200"}`}>
        {value}
      </div>
      {sub && (
        <div className="text-[10px] text-slate-500 mt-0.5">{sub}</div>
      )}
    </div>
  );
}
