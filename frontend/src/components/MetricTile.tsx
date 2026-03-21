interface MetricTileProps {
  label: string;
  value: string | number;
  color?: string;
  sub?: string;
}

export function MetricTile({ label, value, color, sub }: MetricTileProps) {
  return (
    <div className="bg-card px-4 py-3.5 rounded-2xl shadow-card transition-shadow hover:shadow-card-hover">
      <div className="text-[9px] uppercase tracking-[0.12em] text-muted mb-1.5 font-medium">
        {label}
      </div>
      <div className={`text-base font-mono font-bold tracking-tight ${color || "text-primary"}`}>
        {value}
      </div>
      {sub && (
        <div className="text-[10px] text-faint mt-1">{sub}</div>
      )}
    </div>
  );
}
