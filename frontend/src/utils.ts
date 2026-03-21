export function formatNum(value: number, decimals = 4, showSign = false): string {
  if (value == null || isNaN(value)) return "-";
  const prefix = showSign && value >= 0 ? "+" : "";
  return prefix + value.toFixed(decimals);
}

export function pnlColor(value: number): string {
  if (value > 0) return "text-teal-400";
  if (value < 0) return "text-rose-400";
  return "text-slate-400";
}
