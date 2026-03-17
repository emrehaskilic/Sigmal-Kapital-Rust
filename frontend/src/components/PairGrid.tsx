import type { PairSummary } from "../types";
import { formatNum, pnlColor } from "../utils";

interface Props {
  pairs: Record<string, PairSummary>;
}

export function PairGrid({ pairs }: Props) {
  const entries = Object.entries(pairs);
  if (entries.length === 0) return null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
      {entries.map(([sym, p]) => {
        const borderColor =
          p.status === "signal"
            ? p.side === "LONG" ? "border-l-emerald-400" : "border-l-red-400"
            : p.trend === "BULLISH" ? "border-l-emerald-400/40" : "border-l-red-400/40";

        return (
          <div
            key={sym}
            className={`bg-[#0a1628] rounded-xl border border-blue-500/[0.08] border-l-4 ${borderColor} p-3`}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="font-semibold text-slate-200 text-sm">{sym}</span>
              {p.status === "signal" ? (
                <span
                  className={`text-[10px] px-2 py-0.5 rounded-full font-semibold ${
                    p.side === "LONG"
                      ? "bg-emerald-400/15 text-emerald-400"
                      : "bg-red-400/15 text-red-400"
                  }`}
                >
                  {p.side} AKTIF
                </span>
              ) : (
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-blue-500/10 text-slate-400">
                  BEKLIYOR
                </span>
              )}
            </div>
            <div className="font-mono text-lg text-sky-400 mb-1">
              {formatNum(p.last_price, 4)}
            </div>
            {/* Bid / Ask / Spread */}
            <div className="flex gap-3 text-[10px] font-mono mb-2">
              <span className="text-emerald-400/70">B: {formatNum(p.bid, 4)}</span>
              <span className="text-red-400/70">A: {formatNum(p.ask, 4)}</span>
              <span className="text-slate-500">S: {formatNum(p.spread, 6)}</span>
            </div>
            <div className="grid grid-cols-3 gap-2 text-[10px]">
              <div>
                <div className="text-slate-500 uppercase">uPnL</div>
                <div className={`font-mono ${pnlColor(p.unrealized_pnl)}`}>
                  {formatNum(p.unrealized_pnl, 4, true)}
                </div>
              </div>
              <div>
                <div className="text-slate-500 uppercase">rPnL</div>
                <div className={`font-mono ${pnlColor(p.realized_pnl)}`}>
                  {formatNum(p.realized_pnl, 4, true)}
                </div>
              </div>
              <div>
                <div className="text-slate-500 uppercase">Net</div>
                <div className={`font-mono font-semibold ${pnlColor(p.total_pnl)}`}>
                  {formatNum(p.total_pnl, 4, true)}
                </div>
              </div>
            </div>
            <div className="mt-2 text-[10px] text-slate-500 flex gap-3">
              <span>RSI: {p.rsi}</span>
              {p.trend && <span>Trend: {p.trend}</span>}
              <span>Fees: {formatNum(p.fees, 4)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
