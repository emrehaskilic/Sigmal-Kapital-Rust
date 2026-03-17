import type { Position } from "../types";
import { formatNum, pnlColor } from "../utils";

interface Props {
  positions: Position[];
}

export function PositionTable({ positions }: Props) {
  if (positions.length === 0) {
    return (
      <div className="text-slate-500 text-sm p-4 text-center">
        Acik pozisyon yok
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
            <th className="text-left py-2 px-2">Symbol</th>
            <th className="text-center py-2">Side</th>
            <th className="text-right py-2 px-2">Entry</th>
            <th className="text-right py-2 px-2">Bid</th>
            <th className="text-right py-2 px-2">Ask</th>
            <th className="text-right py-2 px-2">Spread</th>
            <th className="text-right py-2 px-2">Mark</th>
            <th className="text-right py-2 px-2">Breakeven</th>
            <th className="text-right py-2 px-2">Notional</th>
            <th className="text-right py-2 px-2">uPnL</th>
            <th className="text-right py-2 px-2">uPnL %</th>
            <th className="text-right py-2 px-2">rPnL</th>
            <th className="text-right py-2 px-2">Net PnL</th>
            <th className="text-right py-2 px-2">Fees</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr
              key={p.symbol}
              className="border-b border-blue-500/[0.06] hover:bg-blue-500/10 transition-colors"
            >
              <td className="py-2 px-2 font-semibold text-slate-200">{p.symbol}</td>
              <td className="py-2 text-center">
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                    p.side === "LONG"
                      ? "bg-emerald-400/15 text-emerald-400"
                      : "bg-red-400/15 text-red-400"
                  }`}
                >
                  {p.side}
                </span>
              </td>
              <td className="py-2 px-2 text-right font-mono">{formatNum(p.entry_price, 4)}</td>
              <td className="py-2 px-2 text-right font-mono text-emerald-400/70">{formatNum(p.bid, 4)}</td>
              <td className="py-2 px-2 text-right font-mono text-red-400/70">{formatNum(p.ask, 4)}</td>
              <td className="py-2 px-2 text-right font-mono text-slate-500">{formatNum(p.spread, 6)}</td>
              <td className="py-2 px-2 text-right font-mono text-slate-300">{formatNum(p.mark_price, 4)}</td>
              <td className="py-2 px-2 text-right font-mono text-amber-400">{formatNum(p.break_even, 4)}</td>
              <td className="py-2 px-2 text-right font-mono text-sky-400">{formatNum(p.notional_usdt, 2)}</td>
              <td className={`py-2 px-2 text-right font-mono font-semibold ${pnlColor(p.unrealized_pnl_usdt)}`}>
                {formatNum(p.unrealized_pnl_usdt, 4, true)}
              </td>
              <td className={`py-2 px-2 text-right font-mono ${pnlColor(p.unrealized_pnl_pct)}`}>
                {formatNum(p.unrealized_pnl_pct, 2, true)}%
              </td>
              <td className={`py-2 px-2 text-right font-mono ${pnlColor(p.realized_pnl_usdt)}`}>
                {formatNum(p.realized_pnl_usdt, 4, true)}
              </td>
              <td className={`py-2 px-2 text-right font-mono font-semibold ${pnlColor(p.total_pnl_usdt)}`}>
                {formatNum(p.total_pnl_usdt, 4, true)}
              </td>
              <td className="py-2 px-2 text-right font-mono text-slate-500">
                {formatNum(p.fees_usdt, 4)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
