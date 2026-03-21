import type { PairSummary } from "../types";
import { formatNum, pnlColor } from "../utils";

interface Props {
  pairs: Record<string, PairSummary>;
}

export function PairGrid({ pairs }: Props) {
  const entries = Object.entries(pairs);
  if (entries.length === 0) return null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
      {entries.map(([sym, p]) => {
        const isActive = p.status === "signal";
        const isLong = p.side === "LONG";

        return (
          <div
            key={sym}
            className={`relative bg-card rounded-2xl p-4 shadow-card transition-all ${
              isActive
                ? isLong
                  ? "ring-1 ring-teal-500/20"
                  : "ring-1 ring-rose-500/20"
                : "hover:shadow-card-hover"
            }`}
          >
            <div className="flex items-center justify-between mb-3">
              <span className="font-semibold text-primary text-sm tracking-tight">{sym}</span>
              {isActive ? (
                <span
                  className={`text-[9px] px-2.5 py-1 rounded-full font-semibold tracking-wide ${
                    isLong
                      ? "bg-teal-500/10 text-teal-400"
                      : "bg-rose-500/10 text-rose-400"
                  }`}
                >
                  {p.side} AKTIF
                </span>
              ) : (
                <span className="text-[9px] px-2.5 py-1 rounded-full bg-black/5 dark:bg-white/5 text-muted tracking-wide">
                  BEKLIYOR
                </span>
              )}
            </div>

            <div className="font-mono text-xl font-bold text-sky-500 dark:text-sky-300 mb-2 tracking-tight">
              {formatNum(p.last_price, 4)}
            </div>

            <div className="flex gap-3 text-[10px] font-mono mb-3 text-muted">
              <span>B: <span className="text-teal-500/60 dark:text-teal-400/60">{formatNum(p.bid, 4)}</span></span>
              <span>A: <span className="text-rose-500/60 dark:text-rose-400/60">{formatNum(p.ask, 4)}</span></span>
              <span>S: {formatNum(p.spread, 6)}</span>
            </div>

            <div className="grid grid-cols-3 gap-3 text-[10px]">
              <div>
                <div className="text-faint uppercase text-[9px] mb-0.5">uPnL</div>
                <div className={`font-mono font-semibold ${pnlColor(p.unrealized_pnl)}`}>
                  {formatNum(p.unrealized_pnl, 4, true)}
                </div>
              </div>
              <div>
                <div className="text-faint uppercase text-[9px] mb-0.5">rPnL</div>
                <div className={`font-mono font-semibold ${pnlColor(p.realized_pnl)}`}>
                  {formatNum(p.realized_pnl, 4, true)}
                </div>
              </div>
              <div>
                <div className="text-faint uppercase text-[9px] mb-0.5">Net</div>
                <div className={`font-mono font-bold ${pnlColor(p.total_pnl)}`}>
                  {formatNum(p.total_pnl, 4, true)}
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-card text-[10px] text-muted flex gap-3">
              <span>RSI: {p.rsi}</span>
              <span>Fees: {formatNum(p.fees, 4)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
