import type { Position } from "../types";
import { formatNum, pnlColor } from "../utils";

interface Props {
  positions: Position[];
}

export function PositionTable({ positions }: Props) {
  if (positions.length === 0) {
    return (
      <div className="text-muted text-sm p-6 text-center">
        Acik pozisyon yok
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[9px] uppercase tracking-[0.1em] text-faint border-b border-card">
            <th className="text-left py-2.5 px-3">Symbol</th>
            <th className="text-center py-2.5">Side</th>
            <th className="text-right py-2.5 px-3">Entry</th>
            <th className="text-right py-2.5 px-3">Bid</th>
            <th className="text-right py-2.5 px-3">Ask</th>
            <th className="text-right py-2.5 px-3">Spread</th>
            <th className="text-right py-2.5 px-3">Mark</th>
            <th className="text-right py-2.5 px-3">Breakeven</th>
            <th className="text-right py-2.5 px-3">Notional</th>
            <th className="text-right py-2.5 px-3">uPnL</th>
            <th className="text-right py-2.5 px-3">uPnL %</th>
            <th className="text-right py-2.5 px-3">rPnL</th>
            <th className="text-right py-2.5 px-3">Net PnL</th>
            <th className="text-right py-2.5 px-3">Fees</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.symbol} className="border-b border-card hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
              <td className="py-2.5 px-3 font-semibold text-primary">{p.symbol}</td>
              <td className="py-2.5 text-center">
                <span className={`px-2.5 py-0.5 rounded-full text-[9px] font-semibold ${
                  p.side === "LONG" ? "bg-teal-500/10 text-teal-400" : "bg-rose-500/10 text-rose-400"
                }`}>{p.side}</span>
              </td>
              <td className="py-2.5 px-3 text-right font-mono text-primary">{formatNum(p.entry_price, 4)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-teal-500/60 dark:text-teal-400/60">{formatNum(p.bid, 4)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-rose-500/60 dark:text-rose-400/60">{formatNum(p.ask, 4)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-muted">{formatNum(p.spread, 6)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-primary">{formatNum(p.mark_price, 4)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-amber-500 dark:text-amber-400/80">{formatNum(p.break_even, 4)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-sky-500 dark:text-sky-300">{formatNum(p.notional_usdt, 2)}</td>
              <td className={`py-2.5 px-3 text-right font-mono font-bold ${pnlColor(p.unrealized_pnl_usdt)}`}>{formatNum(p.unrealized_pnl_usdt, 4, true)}</td>
              <td className={`py-2.5 px-3 text-right font-mono ${pnlColor(p.unrealized_pnl_pct)}`}>{formatNum(p.unrealized_pnl_pct, 2, true)}%</td>
              <td className={`py-2.5 px-3 text-right font-mono ${pnlColor(p.realized_pnl_usdt)}`}>{formatNum(p.realized_pnl_usdt, 4, true)}</td>
              <td className={`py-2.5 px-3 text-right font-mono font-bold ${pnlColor(p.total_pnl_usdt)}`}>{formatNum(p.total_pnl_usdt, 4, true)}</td>
              <td className="py-2.5 px-3 text-right font-mono text-muted">{formatNum(p.fees_usdt, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
