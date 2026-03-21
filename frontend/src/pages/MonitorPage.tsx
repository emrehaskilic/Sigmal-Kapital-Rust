import { useState, useEffect, useRef } from "react";
import { ThemeProvider, useTheme } from "../theme";
import { PMaxChart } from "../components/PMaxChart";

/* ── Dynamic API base — uses page hostname so it works from any device ── */
const API_BASE = `http://${window.location.hostname}:8000`;

async function monitorFetch() {
  const res = await fetch(`${API_BASE}/api/sigmakapital`);
  return res.json();
}

/* ── Helpers ── */
function formatNum(value: number, decimals = 4, showSign = false): string {
  if (value == null || isNaN(value)) return "-";
  const prefix = showSign && value >= 0 ? "+" : "";
  return prefix + value.toFixed(decimals);
}

function pnlColor(value: number): string {
  if (value > 0) return "text-teal-400";
  if (value < 0) return "text-rose-400";
  return "text-slate-400";
}

/* ── Metric Tile (self-contained for monitor) ── */
function MTile({ label, value, color, sub }: { label: string; value: string | number; color?: string; sub?: string }) {
  return (
    <div className="bg-card px-3 py-3 sm:px-4 sm:py-3.5 rounded-2xl shadow-card">
      <div className="text-[8px] sm:text-[9px] uppercase tracking-[0.12em] text-muted mb-1 font-medium">{label}</div>
      <div className={`text-sm sm:text-base font-mono font-bold tracking-tight ${color || "text-primary"}`}>{value}</div>
      {sub && <div className="text-[9px] text-faint mt-0.5">{sub}</div>}
    </div>
  );
}

/* ── Monitor Content ── */
function MonitorContent() {
  const { theme, toggle } = useTheme();
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [activeChart, setActiveChart] = useState<string>("");
  const pollRef = useRef<number | null>(null);

  const fetchMonitorData = async () => {
    try {
      const res = await monitorFetch();
      if (res.error) {
        setError(res.error);
        setData(null);
        return;
      }
      setData(res);
      setError("");
      setLastUpdate(new Date());

      // Auto-select first active symbol for chart
      if (!activeChart && res.active_symbols?.length > 0) {
        setActiveChart(res.active_symbols[0]);
      }
    } catch {
      setError("Sunucuya baglanilamiyor");
    }
  };

  useEffect(() => {
    fetchMonitorData();
    pollRef.current = window.setInterval(fetchMonitorData, 2500);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  // Sync activeChart when symbols change
  useEffect(() => {
    if (data?.active_symbols?.length > 0 && !data.active_symbols.includes(activeChart)) {
      setActiveChart(data.active_symbols[0]);
    }
  }, [data?.active_symbols, activeChart]);

  // Invalid token
  if (error) {
    return (
      <div className="min-h-screen bg-page flex items-center justify-center p-4">
        <div className="bg-card rounded-2xl shadow-card p-8 max-w-md w-full text-center space-y-4">
          <img src="/logo.svg" alt="Sigma Kapital" className="h-12 mx-auto mb-2" draggable={false} />
          <div className="text-rose-400 text-lg font-semibold">{error}</div>
          <p className="text-secondary text-sm">Bu monitor linki gecersiz veya suresi dolmus olabilir.</p>
        </div>
      </div>
    );
  }

  // Loading
  if (!data) {
    return (
      <div className="min-h-screen bg-page flex items-center justify-center">
        <div className="text-muted text-sm animate-pulse">Yukleniyor...</div>
      </div>
    );
  }

  const totals = data.totals || {};
  const stats = data.stats || {};
  const positions = data.positions || [];
  const pairSummaries = data.pair_summaries || {};
  const symbols = data.active_symbols || [];
  const trades = data.trade_log || [];

  return (
    <div className="min-h-screen bg-page text-primary">
      {/* ── Header ── */}
      <header className="bg-nav border-b border-nav px-4 sm:px-6 py-3 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src="/logo.svg" alt="Sigma Kapital" className="h-8 sm:h-10 w-auto object-contain" draggable={false} />
            <div>
              <span className="text-sm sm:text-base font-bold text-primary">Live Monitor</span>
              {data.live_running && (
                <span className="ml-2 inline-flex items-center gap-1 text-[10px] text-emerald-400 font-semibold">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  LIVE
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-3">
            {lastUpdate && (
              <span className="text-[10px] text-muted hidden sm:inline">
                Son guncelleme: {lastUpdate.toLocaleTimeString()}
              </span>
            )}
            {/* Theme toggle */}
            <button
              onClick={toggle}
              className="relative w-8 h-8 rounded-xl bg-black/5 dark:bg-white/5 hover:bg-black/10 dark:hover:bg-white/10 flex items-center justify-center transition-all"
              title={theme === "dark" ? "Light Mode" : "Dark Mode"}
            >
              <svg className={`w-3.5 h-3.5 absolute transition-all duration-300 ${theme === "dark" ? "opacity-0 rotate-90 scale-0" : "opacity-100 rotate-0 scale-100"}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <circle cx="12" cy="12" r="5" /><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
              </svg>
              <svg className={`w-3.5 h-3.5 absolute transition-all duration-300 ${theme === "dark" ? "opacity-100 rotate-0 scale-100" : "opacity-0 -rotate-90 scale-0"}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
              </svg>
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-3 sm:px-6 py-4 sm:py-6 space-y-4 sm:space-y-5">

        {/* Not running state */}
        {!data.live_running && (
          <div className="bg-card rounded-2xl shadow-card p-6 sm:p-8 text-center">
            <div className="text-amber-400 text-lg font-semibold mb-2">Bot Calismıyor</div>
            <p className="text-secondary text-sm">Live trading motoru su anda aktif degil. Veriler bot calistiginda gorunecek.</p>
          </div>
        )}

        {/* ── Balance & Summary ── */}
        {data.live_running && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 sm:gap-3">
            <MTile label="Bakiye" value={`${formatNum(data.balance, 2)} USDT`} color="text-emerald-400" />
            <MTile label="Aktif Pair" value={symbols.length} />
            <MTile label="Toplam Islem" value={stats.total_trades} />
            <MTile label="Win Rate" value={`${formatNum(stats.win_rate, 1)}%`}
              color={stats.win_rate >= 50 ? "text-emerald-400" : "text-rose-400"} />
            <MTile label="Net PnL" value={`${formatNum(totals.net_pnl, 4, true)} USDT`}
              color={pnlColor(totals.net_pnl)} />
            <MTile label="Total Fees" value={`${formatNum(totals.total_fees, 4)} USDT`} color="text-slate-400" />
          </div>
        )}

        {/* ── PnL Breakdown ── */}
        {data.live_running && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 sm:gap-3">
            <MTile label="Unrealized PnL" value={`${formatNum(totals.unrealized_pnl, 4, true)} USDT`} color={pnlColor(totals.unrealized_pnl)} />
            <MTile label="Realized PnL" value={`${formatNum(totals.realized_pnl, 4, true)} USDT`} color={pnlColor(totals.realized_pnl)} />
            <MTile label="Total PnL" value={`${formatNum(totals.total_pnl, 4, true)} USDT`} color={pnlColor(totals.total_pnl)} />
            <MTile label="Bakiye" value={`${formatNum(data.available || 0, 2)} USDT`} color="text-sky-400" sub="Kullanilabilir" />
          </div>
        )}

        {/* ── Pair Status Cards ── */}
        {data.live_running && Object.keys(pairSummaries).length > 0 && (
          <div>
            <h2 className="text-xs sm:text-sm font-semibold text-secondary mb-2 sm:mb-3">Pair Durumlari</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 sm:gap-3">
              {Object.entries(pairSummaries).map(([sym, pair]: [string, any]) => (
                <div key={sym} className={`bg-card rounded-xl shadow-card border p-3 ${
                  pair.pair_state === "OBSERVING"
                    ? "border-amber-500/20"
                    : pair.side === "LONG"
                      ? "border-emerald-500/20"
                      : pair.side === "SHORT"
                        ? "border-rose-500/20"
                        : "border-card"
                }`}>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-sm font-semibold text-primary">{sym}</span>
                    <div className="flex items-center gap-1.5">
                      {pair.pair_state === "OBSERVING" && (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-amber-400/15 text-amber-400">GOZLEM</span>
                      )}
                      {pair.pair_state === "ACTIVE" && (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-emerald-400/15 text-emerald-400">AKTIF</span>
                      )}
                      {pair.side && (
                        <span className={`px-1.5 py-0.5 rounded text-[9px] font-semibold ${
                          pair.side === "LONG" ? "bg-emerald-400/15 text-emerald-400" : "bg-rose-400/15 text-rose-400"
                        }`}>{pair.side}</span>
                      )}
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-1 text-[10px] text-secondary">
                    <div>Fiyat: <span className="text-primary font-mono">{formatNum(pair.last_price, 4)}</span></div>
                    <div>Islem: <span className="text-primary font-mono">{pair.trade_count}</span></div>
                    <div>PnL: <span className={`font-mono ${pnlColor(pair.total_pnl)}`}>{formatNum(pair.total_pnl, 4, true)}</span></div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Chart — Tab per pair ── */}
        {data.live_running && symbols.length > 0 && (
          <div className="space-y-2">
            {symbols.length > 1 && (
              <div className="flex items-center gap-1.5 flex-wrap">
                {symbols.map((sym: string) => (
                  <button
                    key={sym}
                    onClick={() => setActiveChart(sym)}
                    className={`px-2.5 py-1 rounded-lg text-[10px] sm:text-[11px] font-semibold transition-all ${
                      sym === activeChart
                        ? "bg-teal-500/15 text-teal-400 ring-1 ring-teal-500/20"
                        : "bg-card text-secondary hover:text-primary"
                    }`}
                  >
                    {sym.replace("USDT", "")}
                  </button>
                ))}
              </div>
            )}
            <PMaxChart
              key={activeChart}
              symbol={activeChart}
              botRunning={true}
              title={`${activeChart} — Trend Inventory`}
              mode="live"
            />
          </div>
        )}

        {/* ── Positions Table ── */}
        {data.live_running && positions.length > 0 && (
          <div className="bg-card rounded-2xl shadow-card p-3 sm:p-4">
            <h2 className="text-xs sm:text-sm font-semibold text-emerald-400 mb-2 sm:mb-3">Acik Pozisyonlar</h2>
            <div className="overflow-x-auto -mx-3 sm:mx-0">
              <table className="w-full text-[10px] sm:text-xs min-w-[600px]">
                <thead>
                  <tr className="text-[8px] sm:text-[9px] uppercase tracking-[0.1em] text-faint border-b border-card">
                    <th className="text-left py-2 px-2 sm:px-3">Symbol</th>
                    <th className="text-center py-2">Side</th>
                    <th className="text-right py-2 px-2 sm:px-3">Entry</th>
                    <th className="text-right py-2 px-2 sm:px-3">Mark</th>
                    <th className="text-right py-2 px-2 sm:px-3">Notional</th>
                    <th className="text-right py-2 px-2 sm:px-3">uPnL</th>
                    <th className="text-right py-2 px-2 sm:px-3">uPnL %</th>
                    <th className="text-right py-2 px-2 sm:px-3">rPnL</th>
                    <th className="text-right py-2 px-2 sm:px-3">Fees</th>
                    <th className="text-center py-2 px-2">DCA</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p: any) => (
                    <tr key={p.symbol} className="border-b border-card">
                      <td className="py-2 px-2 sm:px-3 font-semibold text-primary">{p.symbol}</td>
                      <td className="py-2 text-center">
                        <span className={`px-1.5 sm:px-2.5 py-0.5 rounded-full text-[8px] sm:text-[9px] font-semibold ${
                          p.side === "LONG" ? "bg-teal-500/10 text-teal-400" : "bg-rose-500/10 text-rose-400"
                        }`}>{p.side}</span>
                      </td>
                      <td className="py-2 px-2 sm:px-3 text-right font-mono text-primary">{formatNum(p.entry_price, 4)}</td>
                      <td className="py-2 px-2 sm:px-3 text-right font-mono text-primary">{formatNum(p.mark_price, 4)}</td>
                      <td className="py-2 px-2 sm:px-3 text-right font-mono text-sky-400">{formatNum(p.notional_usdt, 2)}</td>
                      <td className={`py-2 px-2 sm:px-3 text-right font-mono font-bold ${pnlColor(p.unrealized_pnl_usdt)}`}>{formatNum(p.unrealized_pnl_usdt, 4, true)}</td>
                      <td className={`py-2 px-2 sm:px-3 text-right font-mono ${pnlColor(p.unrealized_pnl_pct)}`}>{formatNum(p.unrealized_pnl_pct, 2, true)}%</td>
                      <td className={`py-2 px-2 sm:px-3 text-right font-mono ${pnlColor(p.realized_pnl_usdt)}`}>{formatNum(p.realized_pnl_usdt, 4, true)}</td>
                      <td className="py-2 px-2 sm:px-3 text-right font-mono text-muted">{formatNum(p.fees_usdt, 4)}</td>
                      <td className="py-2 px-2 text-center font-mono text-amber-400">{p.dca_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Trade History ── */}
        {data.live_running && trades.length > 0 && (
          <div className="bg-card rounded-2xl shadow-card p-3 sm:p-4">
            <h2 className="text-xs sm:text-sm font-semibold text-secondary mb-2 sm:mb-3">Islem Gecmisi</h2>
            <div className="overflow-x-auto -mx-3 sm:mx-0">
              <table className="w-full text-[10px] sm:text-xs min-w-[550px]">
                <thead>
                  <tr className="text-[8px] sm:text-[9px] uppercase tracking-[0.1em] text-faint border-b border-card">
                    <th className="text-left py-2 px-2 sm:px-3">#</th>
                    <th className="text-left py-2 px-2 sm:px-3">Symbol</th>
                    <th className="text-center py-2">Side</th>
                    <th className="text-right py-2 px-2 sm:px-3">Entry</th>
                    <th className="text-right py-2 px-2 sm:px-3">Exit</th>
                    <th className="text-center py-2 px-2 sm:px-3">Reason</th>
                    <th className="text-right py-2 px-2 sm:px-3">PnL</th>
                    <th className="text-right py-2 px-2 sm:px-3">Fee</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.slice(-50).reverse().map((t: any) => (
                    <tr key={t.id} className="border-b border-card">
                      <td className="py-1.5 px-2 sm:px-3 text-muted">{t.id}</td>
                      <td className="py-1.5 px-2 sm:px-3 font-semibold text-primary">{t.symbol}</td>
                      <td className="py-1.5 text-center">
                        <span className={`px-1.5 py-0.5 rounded-full text-[8px] sm:text-[9px] font-semibold ${
                          t.side === "LONG" ? "bg-teal-500/10 text-teal-400" : "bg-rose-500/10 text-rose-400"
                        }`}>{t.side}</span>
                      </td>
                      <td className="py-1.5 px-2 sm:px-3 text-right font-mono text-primary">{formatNum(t.entry_price, 4)}</td>
                      <td className="py-1.5 px-2 sm:px-3 text-right font-mono text-primary">{formatNum(t.exit_price, 4)}</td>
                      <td className="py-1.5 px-2 sm:px-3 text-center">
                        <span className={`text-[8px] sm:text-[9px] px-1.5 sm:px-2.5 py-0.5 rounded-full font-semibold ${
                          t.exit_reason === "SL" || t.exit_reason === "PCT_STOP" || t.exit_reason === "HARD_STOP"
                            ? "bg-rose-500/10 text-rose-400"
                            : t.exit_reason === "REVERSAL" || t.exit_reason === "REVERSAL_CLOSE"
                              ? "bg-amber-500/10 text-amber-400"
                              : "bg-teal-500/10 text-teal-400"
                        }`}>{t.exit_reason}</span>
                      </td>
                      <td className={`py-1.5 px-2 sm:px-3 text-right font-mono font-bold ${pnlColor(t.pnl_usdt)}`}>{formatNum(t.pnl_usdt, 4, true)}</td>
                      <td className="py-1.5 px-2 sm:px-3 text-right font-mono text-muted">{formatNum(t.fee_usdt, 4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Footer ── */}
        <div className="text-center text-[10px] text-muted pb-4 pt-2">
          Sigma Kapital Trading Technologies & Market Making Services
        </div>
      </main>
    </div>
  );
}

/* ── MonitorPage wrapper ── */
export default function MonitorPage() {
  return (
    <ThemeProvider>
      <MonitorContent />
    </ThemeProvider>
  );
}
