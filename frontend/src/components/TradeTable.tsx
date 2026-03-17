import type { TradeLog } from "../types";
import { formatNum, pnlColor } from "../utils";

interface Props {
  trades: TradeLog[];
}

export function TradeTable({ trades }: Props) {
  if (trades.length === 0) {
    return (
      <div className="text-slate-500 text-sm p-4 text-center">
        Henuz islem yapilmadi
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
            <th className="text-left py-2 px-2">#</th>
            <th className="text-left py-2 px-2">Symbol</th>
            <th className="text-center py-2">Side</th>
            <th className="text-right py-2 px-2">Entry</th>
            <th className="text-right py-2 px-2">Exit</th>
            <th className="text-center py-2 px-2">Reason</th>
            <th className="text-right py-2 px-2">PnL USDT</th>
            <th className="text-right py-2 px-2">PnL %</th>
            <th className="text-right py-2 px-2">Fee</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr
              key={t.id}
              className="border-b border-blue-500/[0.06] hover:bg-blue-500/10 transition-colors"
            >
              <td className="py-1.5 px-2 text-slate-500">{t.id}</td>
              <td className="py-1.5 px-2 font-semibold text-slate-200">{t.symbol}</td>
              <td className="py-1.5 text-center">
                <span
                  className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                    t.side === "LONG"
                      ? "bg-emerald-400/15 text-emerald-400"
                      : "bg-red-400/15 text-red-400"
                  }`}
                >
                  {t.side}
                </span>
              </td>
              <td className="py-1.5 px-2 text-right font-mono">{formatNum(t.entry_price, 4)}</td>
              <td className="py-1.5 px-2 text-right font-mono">{formatNum(t.exit_price, 4)}</td>
              <td className="py-1.5 px-2 text-center">
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded ${
                    t.exit_reason === "SL"
                      ? "bg-red-400/15 text-red-400"
                      : "bg-emerald-400/15 text-emerald-400"
                  }`}
                >
                  {t.exit_reason}
                </span>
              </td>
              <td className={`py-1.5 px-2 text-right font-mono font-semibold ${pnlColor(t.pnl_usdt)}`}>
                {formatNum(t.pnl_usdt, 4, true)}
              </td>
              <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(t.pnl_pct)}`}>
                {formatNum(t.pnl_pct, 2, true)}%
              </td>
              <td className="py-1.5 px-2 text-right font-mono text-slate-500">
                {formatNum(t.fee_usdt, 4)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
