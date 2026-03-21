import type { TradeLog } from "../types";
import { formatNum, pnlColor } from "../utils";

interface Props {
  trades: TradeLog[];
}

export function TradeTable({ trades }: Props) {
  if (trades.length === 0) {
    return (
      <div className="text-muted text-sm p-6 text-center">
        Henuz islem yapilmadi
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[9px] uppercase tracking-[0.1em] text-faint border-b border-card">
            <th className="text-left py-2.5 px-3">#</th>
            <th className="text-left py-2.5 px-3">Symbol</th>
            <th className="text-center py-2.5">Side</th>
            <th className="text-right py-2.5 px-3">Entry</th>
            <th className="text-right py-2.5 px-3">Exit</th>
            <th className="text-center py-2.5 px-3">Reason</th>
            <th className="text-right py-2.5 px-3">PnL USDT</th>
            <th className="text-right py-2.5 px-3">PnL %</th>
            <th className="text-right py-2.5 px-3">Fee</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id} className="border-b border-card hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
              <td className="py-2 px-3 text-muted">{t.id}</td>
              <td className="py-2 px-3 font-semibold text-primary">{t.symbol}</td>
              <td className="py-2 text-center">
                <span className={`px-2.5 py-0.5 rounded-full text-[9px] font-semibold ${
                  t.side === "LONG" ? "bg-teal-500/10 text-teal-400" : "bg-rose-500/10 text-rose-400"
                }`}>{t.side}</span>
              </td>
              <td className="py-2 px-3 text-right font-mono text-primary">{formatNum(t.entry_price, 4)}</td>
              <td className="py-2 px-3 text-right font-mono text-primary">{formatNum(t.exit_price, 4)}</td>
              <td className="py-2 px-3 text-center">
                <span className={`text-[9px] px-2.5 py-0.5 rounded-full font-semibold ${
                  t.exit_reason === "SL" || t.exit_reason === "PCT_STOP" || t.exit_reason === "HARD_STOP"
                    ? "bg-rose-500/10 text-rose-400"
                    : t.exit_reason === "REVERSAL" || t.exit_reason === "REVERSAL_CLOSE"
                      ? "bg-amber-500/10 text-amber-400"
                      : "bg-teal-500/10 text-teal-400"
                }`}>{t.exit_reason}</span>
              </td>
              <td className={`py-2 px-3 text-right font-mono font-bold ${pnlColor(t.pnl_usdt)}`}>{formatNum(t.pnl_usdt, 4, true)}</td>
              <td className={`py-2 px-3 text-right font-mono ${pnlColor(t.pnl_pct)}`}>{formatNum(t.pnl_pct, 2, true)}%</td>
              <td className="py-2 px-3 text-right font-mono text-muted">{formatNum(t.fee_usdt, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
