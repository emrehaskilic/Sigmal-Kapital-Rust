import { useState, useEffect, useRef, useMemo } from "react";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, BarChart, Bar, ReferenceLine, Cell,
  ScatterChart, Scatter, ZAxis, LineChart, Line,
} from "recharts";
import {
  fetchSymbols, fetchConfig, runBacktest,
  fetchBacktestStatus, fetchBacktestResults, resetBacktest,
} from "../api";
import type { Config, BacktestResult, BacktestTrade } from "../types";
import { MetricTile } from "../components/MetricTile";
import { formatNum, pnlColor } from "../utils";
import { exportBacktestPdf } from "../utils/exportPdf";

/* ── date formatters ── */
const fmtDate = (ts: number) => {
  const d = new Date(ts);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}/${dd} ${hh}:${mi}`;
};
const fmtDateTime = (ts: number) => {
  const d = new Date(ts);
  const yy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${yy}-${mm}-${dd} ${hh}:${mi}`;
};

const DAYS_TR = ["Pazar", "Pazartesi", "Sali", "Carsamba", "Persembe", "Cuma", "Cumartesi"];
const MONTHS_TR = ["Oca", "Sub", "Mar", "Nis", "May", "Haz", "Tem", "Agu", "Eyl", "Eki", "Kas", "Ara"];

/* ── downsample: keep max N points using LTTB-like uniform sampling ── */
function downsample<T>(data: T[], maxPoints: number): T[] {
  if (data.length <= maxPoints) return data;
  const step = (data.length - 2) / (maxPoints - 2);
  const out: T[] = [data[0]];
  for (let i = 1; i < maxPoints - 1; i++) {
    out.push(data[Math.round(i * step)]);
  }
  out.push(data[data.length - 1]);
  return out;
}

const TRADES_PER_PAGE = 50;

/* ── Section wrapper ── */
const Section = ({ title, children, className = "" }: { title: string; children: React.ReactNode; className?: string }) => (
  <div className={`bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4 ${className}`}>
    <h2 className="text-sm font-semibold text-slate-300 mb-3">{title}</h2>
    {children}
  </div>
);

export default function BacktestPage() {
  /* ── state ── */
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [config, setConfig] = useState<Config | null>(null);
  const [lookbackDays, setLookbackDays] = useState(30);

  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [btStatus, setBtStatus] = useState("idle");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tradeFilter, setTradeFilter] = useState<string>("ALL");
  const [tradePage, setTradePage] = useState(0);

  // (history/compare removed)

  const pollRef = useRef<number | null>(null);

  /* ── init ── */
  useEffect(() => {
    fetchSymbols().then((d) => setAllSymbols(d.symbols));
    fetchConfig().then((c) => setConfig(c));
  }, []);

  /* ── poll backtest progress ── */
  useEffect(() => {
    if (!running) return;
    const poll = async () => {
      try {
        const s = await fetchBacktestStatus();
        setProgress(s.progress);
        setBtStatus(s.status);
        if (!s.running) {
          setRunning(false);
          if (s.error) {
            setError(s.error);
          } else {
            const r = await fetchBacktestResults();
            if (r.metrics) {
              setResult(r);
            }
          }
        }
      } catch { /* ignore */ }
    };
    poll();
    pollRef.current = window.setInterval(poll, 1000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [running]);

  /* ── handlers ── */
  const handleAddPair = (sym: string) => {
    if (selectedSymbols.length >= 50) return;
    setSelectedSymbols((prev) => [...prev, sym]);
    setSearchQuery("");
  };
  const handleRemovePair = (sym: string) => {
    setSelectedSymbols((prev) => prev.filter((s) => s !== sym));
  };
  const handleConfigChange = (section: string, key: string, value: number | string | boolean) => {
    if (!config) return;
    setConfig({ ...config, [section]: { ...(config as any)[section], [key]: value } });
  };
  const handleTfChange = (tfIdx: number, path: string, key: string, value: number | string | boolean) => {
    if (!config) return;
    const tfs = [...config.strategy.timeframes];
    const tf = { ...tfs[tfIdx] } as any;
    if (path) {
      tf[path] = { ...tf[path], [key]: value };
    } else {
      tf[key] = value;
    }
    tfs[tfIdx] = tf;
    setConfig({ ...config, strategy: { ...config.strategy, timeframes: tfs } });
  };
  const handleRun = async () => {
    if (selectedSymbols.length === 0 || !config) return;
    setRunning(true);
    setProgress(0);
    setResult(null);
    setError(null);
    setBtStatus("starting");
    setTradeFilter("ALL");
    await runBacktest(selectedSymbols, lookbackDays, config);
  };

  const handleReset = async () => {
    await resetBacktest();
    setResult(null);
    setError(null);
    setProgress(0);
    setBtStatus("idle");
    setTradeFilter("ALL");
  };

  /* ── Export to CSV ── */
  const handleExportCSV = () => {
    if (!result) return;
    const headers = ["ID","Symbol","Side","Entry Time","Entry Price","Exit Time","Exit Price","Exit Reason","PnL USDT","PnL %","Fee USDT","Leverage"];
    const rows = result.trades.map(t => [
      t.id, t.symbol, t.side,
      t.entry_time ? fmtDateTime(t.entry_time) : "",
      t.entry_price, t.exit_time ? fmtDateTime(t.exit_time) : "",
      t.exit_price, t.exit_reason, t.pnl_usdt, t.pnl_pct, t.fee_usdt, t.leverage,
    ]);
    const csv = [headers.join(","), ...rows.map(r => r.join(","))].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `backtest_trades_${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  /* ── Export full report as JSON ── */
  const handleExportJSON = () => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `backtest_report_${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const filteredSymbols = allSymbols
    .filter((s) => !selectedSymbols.includes(s))
    .filter((s) => s.toLowerCase().includes(searchQuery.toLowerCase()));

  const m = result?.metrics;

  /* ── Derived data for new charts ── */
  const monthlyReturns = useMemo(() => {
    if (!result?.trades.length) return [];
    const map = new Map<string, number>();
    for (const t of result.trades) {
      if (!t.exit_time) continue;
      const d = new Date(t.exit_time);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      map.set(key, (map.get(key) || 0) + t.pnl_usdt);
    }
    return Array.from(map.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([month, pnl]) => ({ month, pnl: +pnl.toFixed(2) }));
  }, [result]);

  const pnlDistribution = useMemo(() => {
    if (!result?.trades.length) return [];
    const pnls = result.trades.map(t => t.pnl_usdt);
    const min = Math.min(...pnls);
    const max = Math.max(...pnls);
    const range = max - min;
    if (range === 0) return [{ bin: "0", binCenter: 0, count: pnls.length }];
    const bucketCount = Math.min(25, Math.max(8, Math.ceil(Math.sqrt(pnls.length))));
    const step = range / bucketCount;
    const buckets = Array.from({ length: bucketCount }, (_, i) => ({
      bin: `${(min + i * step).toFixed(1)}`,
      binCenter: min + (i + 0.5) * step,
      count: 0,
    }));
    for (const p of pnls) {
      const idx = Math.min(Math.floor((p - min) / step), bucketCount - 1);
      buckets[idx].count++;
    }
    return buckets;
  }, [result]);

  const cumulativePnlBySymbol = useMemo(() => {
    if (!result?.trades.length) return [];
    const syms = [...new Set(result.trades.map(t => t.symbol))].sort();
    const sorted = [...result.trades].sort((a, b) => (a.exit_time || 0) - (b.exit_time || 0));
    const cumMap: Record<string, number> = {};
    syms.forEach(s => { cumMap[s] = 0; });
    const points: Record<string, any>[] = [];
    for (const t of sorted) {
      cumMap[t.symbol] = (cumMap[t.symbol] || 0) + t.pnl_usdt;
      const point: any = { time: t.exit_time || t.entry_time };
      syms.forEach(s => { point[s] = +cumMap[s].toFixed(2); });
      points.push(point);
    }
    return { points: downsample(points, 500), symbols: syms };
  }, [result]);

  const durationVsPnl = useMemo(() => {
    if (!result?.trades.length) return [];
    const all = result.trades
      .filter(t => t.entry_time && t.exit_time && t.exit_time > t.entry_time)
      .map(t => ({
        duration: +((t.exit_time - t.entry_time) / 60000).toFixed(1),
        pnl: +t.pnl_usdt.toFixed(4),
        symbol: t.symbol,
        side: t.side,
      }));
    return downsample(all, 500);
  }, [result]);

  const rollingWinRate = useMemo(() => {
    if (!result?.trades.length || result.trades.length < 10) return [];
    const window = Math.min(20, Math.floor(result.trades.length / 2));
    const points: { idx: number; winRate: number }[] = [];
    for (let i = window - 1; i < result.trades.length; i++) {
      const slice = result.trades.slice(i - window + 1, i + 1);
      const wins = slice.filter(t => t.pnl_usdt > 0).length;
      points.push({ idx: i + 1, winRate: +(wins / window * 100).toFixed(1) });
    }
    return downsample(points, 500);
  }, [result]);

  /* ── custom tooltip ── */
  const ChartTooltip = ({ active, payload, label }: any) => {
    if (!active || !payload?.length) return null;
    return (
      <div className="bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-2 text-xs">
        <p className="text-slate-400 mb-1">{typeof label === "number" && label > 1e9 ? fmtDate(label) : label}</p>
        {payload.map((p: any) => (
          <p key={p.dataKey} style={{ color: p.color }}>
            {p.name}: {formatNum(p.value, 2)}
            {p.dataKey.includes("pct") || p.dataKey === "winRate" ? "%" : p.dataKey === "equity" ? " USDT" : ""}
          </p>
        ))}
      </div>
    );
  };

  const cm: any = null; // comparison removed

  return (
    <div className="min-h-screen bg-[#050a14] text-slate-200 p-4 md:p-6">
      <div className="max-w-7xl mx-auto space-y-5">

        {/* ── Header ── */}
        <div>
          <h1 className="text-xl font-bold text-slate-100">Backtest</h1>
          <span className="text-[11px] text-slate-500">Historical simulation using the same signal + risk engine</span>
        </div>

        {/* ── Config Panel ── */}
        {config && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4 space-y-4">
            {/* Trading */}
            <div>
              <h2 className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-2">Trading</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 text-xs">
                {[
                  { section: "trading", key: "initial_balance", label: "Ana Kasa (USDT)", step: "10" },
                  { section: "trading", key: "margin_per_trade", label: "Margin/Trade (USDT)", step: "10" },
                  { section: "trading", key: "leverage", label: "Kaldirac", step: "1" },
                ].map(({ section, key, label, step }) => (
                  <label key={key} className="space-y-1">
                    <span className="text-slate-500 text-[10px] uppercase">{label}</span>
                    <input type="number" step={step}
                      value={(config as any)[section][key]}
                      onChange={(e) => handleConfigChange(section, key, +e.target.value)}
                      className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-2 py-1 text-sm font-mono text-slate-200" />
                  </label>
                ))}
                <label className="space-y-1">
                  <span className="text-slate-500 text-[10px] uppercase">Islem Tipi</span>
                  <select value={config.trading.trade_type}
                    onChange={(e) => handleConfigChange("trading", "trade_type", e.target.value)}
                    className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-2 py-1 text-sm text-slate-200">
                    {["BOTH", "LONG", "SHORT"].map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </label>
              </div>
            </div>

            {/* Adaptive PMax Info (read-only) */}
            <div>
              <div className="flex items-center gap-2 mb-1">
                <h2 className="text-[10px] font-semibold text-slate-500 uppercase tracking-wider">
                  Adaptive PMax — {config.strategy.timeframes[0]?.label || "3m"}
                </h2>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-mono">
                  Dinamik Parametreler
                </span>
              </div>
              <p className="text-[10px] text-slate-500">
                Parametreler volatiliteye gore otomatik ayarlanir (settings.yaml)
              </p>
            </div>
          </div>
        )}

        {/* ── Pair Selection + Controls ── */}
        <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <h2 className="text-sm font-semibold text-slate-300">Pair Secimi</h2>
            <span className="text-[10px] text-slate-500">{selectedSymbols.length}/50</span>
            <div className="flex-1" />
            <label className="flex items-center gap-2 text-xs">
              <span className="text-slate-400">Lookback</span>
              <input type="number" min={1} max={365} step={1}
                value={lookbackDays}
                onChange={(e) => setLookbackDays(+e.target.value)}
                className="w-16 bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-2 py-1 text-sm font-mono text-slate-200" />
              <span className="text-slate-500">gun</span>
            </label>
            {result && !running && (
              <button onClick={handleReset}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-slate-600/15 text-slate-300 border border-slate-600/25 hover:bg-red-500/15 hover:text-red-400 hover:border-red-500/25 transition-colors">
                Sifirla
              </button>
            )}
            <button onClick={handleRun}
              disabled={selectedSymbols.length === 0 || running || !!result}
              className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-sky-500/15 text-sky-400 border border-sky-500/25 hover:bg-sky-500/25 transition-colors disabled:opacity-30 disabled:cursor-not-allowed">
              {running ? "Calisiyor..." : "Backtest Baslat"}
            </button>
          </div>

          <div className="flex gap-3 mb-3">
            <div className="relative flex-1 max-w-xs">
              <input type="text" value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Pair ara... (BTCUSDT)"
                className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-sky-500/50" />
              {searchQuery && filteredSymbols.length > 0 && (
                <div className="absolute z-10 top-full left-0 right-0 mt-1 bg-[#050a14] border border-blue-500/[0.08] rounded-lg max-h-48 overflow-y-auto">
                  {filteredSymbols.slice(0, 20).map((s) => (
                    <button key={s} onClick={() => handleAddPair(s)}
                      className="w-full text-left px-3 py-1.5 text-xs text-slate-300 hover:bg-blue-500/10 transition-colors">
                      {s}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {selectedSymbols.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {selectedSymbols.map((s) => (
                <button key={s} onClick={() => handleRemovePair(s)}
                  className="flex items-center gap-1 px-2 py-1 rounded bg-blue-500/10 text-xs text-slate-300 hover:bg-red-500/20 hover:text-red-400 transition-colors">
                  {s} <span className="text-slate-500 hover:text-red-400">&times;</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Progress Bar ── */}
        {running && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
            <div className="flex items-center justify-between text-xs mb-2">
              <span className="text-slate-400">{btStatus === "fetching" ? "Veri indiriliyor..." : btStatus === "computing" ? "Sinyaller hesaplaniyor..." : btStatus === "simulating" ? "Simulasyon calisiyor..." : "Metrikler hesaplaniyor..."}</span>
              <span className="text-slate-500 font-mono">{progress.toFixed(0)}%</span>
            </div>
            <div className="w-full h-2 bg-blue-500/20 rounded-full overflow-hidden">
              <div className="h-full bg-sky-500 transition-all duration-300 rounded"
                style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {/* ── Error ── */}
        {error && (
          <div className="bg-red-500/10 border border-red-500/25 rounded-xl p-3 text-xs text-red-400">
            Backtest hatasi: {error}
          </div>
        )}

        {/* ══════════════════════════════════════════════════════════════════
            RESULTS
        ══════════════════════════════════════════════════════════════════ */}
        {m && (
          <>
            {/* ── Export & History Bar ── */}
            <div className="flex items-center gap-3 flex-wrap">
              <button onClick={() => result && exportBacktestPdf(result)}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-blue-500/15 text-blue-400 border border-blue-500/25 hover:bg-blue-500/25 transition-colors">
                PDF Rapor Indir
              </button>
              <button onClick={handleExportCSV}
                className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/20 transition-colors">
                CSV Indir
              </button>
              <button onClick={handleExportJSON}
                className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-sky-500/10 text-sky-400 border border-sky-500/20 hover:bg-sky-500/20 transition-colors">
                JSON Rapor Indir
              </button>
              <div className="flex-1" />
            </div>

            {/* ── Summary Metrics Row 1 ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Total PnL" value={`${formatNum(m.total_pnl, 2, true)} USDT`} color={pnlColor(m.total_pnl)}
                sub={cm ? `vs ${formatNum(cm.total_pnl, 2, true)}` : undefined} />
              <MetricTile label="Total PnL %" value={`${formatNum(m.total_pnl_pct, 2, true)}%`} color={pnlColor(m.total_pnl_pct)}
                sub={cm ? `vs ${formatNum(cm.total_pnl_pct, 2, true)}%` : undefined} />
              <MetricTile label="Net (- Fees)" value={`${formatNum(m.total_pnl - m.total_fees, 2, true)} USDT`} color={pnlColor(m.total_pnl - m.total_fees)} />
              <MetricTile label="Bakiye" value={`${formatNum(m.current_balance, 2)} USDT`} color="text-sky-400" />
              <MetricTile label="Max Drawdown" value={`${formatNum(m.max_drawdown_pct, 2)}%`} color="text-red-400"
                sub={cm ? `vs ${formatNum(cm.max_drawdown_pct, 2)}%` : undefined} />
              <MetricTile label="Profit Factor" value={formatNum(m.profit_factor, 2)} color={m.profit_factor >= 1 ? "text-emerald-400" : "text-red-400"}
                sub={cm ? `vs ${formatNum(cm.profit_factor, 2)}` : undefined} />
            </div>

            {/* ── Summary Metrics Row 2 ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Toplam Islem" value={m.total_trades}
                sub={cm ? `vs ${cm.total_trades}` : undefined} />
              <MetricTile label="Kazanan" value={m.winning_trades} color="text-emerald-400" />
              <MetricTile label="Kaybeden" value={m.losing_trades} color="text-red-400" />
              <MetricTile label="Win Rate" value={`${formatNum(m.win_rate, 1)}%`} color={m.win_rate >= 50 ? "text-emerald-400" : "text-red-400"}
                sub={cm ? `vs ${formatNum(cm.win_rate, 1)}%` : undefined} />
              <MetricTile label="Avg Win" value={`${formatNum(m.avg_win, 2)} USDT`} color="text-emerald-400" />
              <MetricTile label="Avg Loss" value={`${formatNum(m.avg_loss, 2)} USDT`} color="text-red-400" />
            </div>

            {/* ── Summary Metrics Row 3 (existing + new) ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Gross Profit" value={`${formatNum(m.gross_profit, 2)} USDT`} color="text-emerald-400" />
              <MetricTile label="Gross Loss" value={`${formatNum(m.gross_loss, 2)} USDT`} color="text-red-400" />
              <MetricTile label="Max Run-up" value={`${formatNum(m.max_runup_pct, 2)}%`} color="text-emerald-400" />
              <MetricTile label="Max DD (USDT)" value={`${formatNum(m.max_drawdown_usdt, 2)} USDT`} color="text-red-400" />
              <MetricTile label="Sharpe Ratio" value={formatNum(m.sharpe_ratio, 3)} color={m.sharpe_ratio >= 0 ? "text-emerald-400" : "text-red-400"} />
              <MetricTile label="Toplam Fee" value={`${formatNum(m.total_fees, 2)} USDT`} color="text-slate-400" />
            </div>

            {/* ── Summary Metrics Row 4 (NEW metrics) ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Sortino Ratio" value={formatNum(m.sortino_ratio, 3)} color={m.sortino_ratio >= 0 ? "text-emerald-400" : "text-red-400"} />
              <MetricTile label="Calmar Ratio" value={formatNum(m.calmar_ratio, 3)} color={m.calmar_ratio >= 0 ? "text-emerald-400" : "text-red-400"} />
              <MetricTile label="Recovery Factor" value={formatNum(m.recovery_factor, 2)} color={m.recovery_factor >= 1 ? "text-emerald-400" : "text-red-400"} />
              <MetricTile label="Expectancy" value={`${formatNum(m.expectancy, 2)} USDT`} color={pnlColor(m.expectancy)} />
              <MetricTile label="En Iyi Islem" value={`${formatNum(m.best_trade_pnl, 2)} USDT`} color="text-emerald-400" />
              <MetricTile label="En Kotu Islem" value={`${formatNum(m.worst_trade_pnl, 2)} USDT`} color="text-red-400" />
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Seri Kazanma" value={m.max_consecutive_wins} color="text-emerald-400" />
              <MetricTile label="Seri Kaybetme" value={m.max_consecutive_losses} color="text-red-400" />
              <MetricTile label="Ort. Islem Suresi" value={`${m.avg_duration_min} dk`} color="text-slate-300" />
            </div>

            {/* ── Per-Symbol Metrics ── */}
            {result!.per_symbol && result!.per_symbol.length > 0 && (
              <Section title="Parite Bazinda Sonuclar">
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                        <th className="text-left py-2 px-2">Symbol</th>
                        <th className="text-right py-2 px-2">Islem</th>
                        <th className="text-right py-2 px-2">Kazanan</th>
                        <th className="text-right py-2 px-2">Kaybeden</th>
                        <th className="text-right py-2 px-2">Win Rate</th>
                        <th className="text-right py-2 px-2">PnL (USDT)</th>
                        <th className="text-right py-2 px-2">Fees</th>
                        <th className="text-right py-2 px-2">Net PnL</th>
                        <th className="text-right py-2 px-2">PF</th>
                        <th className="text-right py-2 px-2">Avg Win</th>
                        <th className="text-right py-2 px-2">Avg Loss</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result!.per_symbol.map((ps) => (
                        <tr key={ps.symbol} className="border-b border-blue-500/[0.06] hover:bg-blue-500/10/30">
                          <td className="py-1.5 px-2 font-semibold">{ps.symbol}</td>
                          <td className="py-1.5 px-2 text-right">{ps.total_trades}</td>
                          <td className="py-1.5 px-2 text-right text-emerald-400">{ps.winning_trades}</td>
                          <td className="py-1.5 px-2 text-right text-red-400">{ps.losing_trades}</td>
                          <td className={`py-1.5 px-2 text-right ${ps.win_rate >= 50 ? "text-emerald-400" : "text-red-400"}`}>
                            {formatNum(ps.win_rate, 1)}%
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(ps.total_pnl)}`}>
                            {formatNum(ps.total_pnl, 2, true)}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-slate-500">
                            {formatNum(ps.total_fees, 2)}
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(ps.total_pnl - ps.total_fees)}`}>
                            {formatNum(ps.total_pnl - ps.total_fees, 2, true)}
                          </td>
                          <td className={`py-1.5 px-2 text-right ${ps.profit_factor >= 1 ? "text-emerald-400" : "text-red-400"}`}>
                            {formatNum(ps.profit_factor, 2)}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-emerald-400">
                            {formatNum(ps.avg_win, 2)}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-red-400">
                            {formatNum(ps.avg_loss, 2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Section>
            )}

            {/* ── Long vs Short Breakdown ── */}
            {result!.trades.length > 0 && (() => {
              const longs = result!.trades.filter(t => t.side === "LONG");
              const shorts = result!.trades.filter(t => t.side === "SHORT");
              const calc = (arr: BacktestTrade[]) => {
                const wins = arr.filter(t => t.pnl_usdt > 0);
                const losses = arr.filter(t => t.pnl_usdt < 0);
                const gp = wins.reduce((s, t) => s + t.pnl_usdt, 0);
                const gl = Math.abs(losses.reduce((s, t) => s + t.pnl_usdt, 0));
                return {
                  count: arr.length,
                  wins: wins.length,
                  losses: losses.length,
                  winRate: arr.length ? +(wins.length / arr.length * 100).toFixed(1) : 0,
                  totalPnl: +arr.reduce((s, t) => s + t.pnl_usdt, 0).toFixed(2),
                  avgPnl: arr.length ? +(arr.reduce((s, t) => s + t.pnl_usdt, 0) / arr.length).toFixed(2) : 0,
                  pf: gl > 0 ? +(gp / gl).toFixed(2) : (gp > 0 ? 999.99 : 0),
                };
              };
              const ld = calc(longs);
              const sd = calc(shorts);

              return (
                <Section title="Long vs Short Analizi">
                  <div className="grid grid-cols-2 gap-4">
                    {[
                      { label: "LONG", data: ld, color: "emerald" },
                      { label: "SHORT", data: sd, color: "red" },
                    ].map(({ label, data, color }) => (
                      <div key={label} className={`bg-[#050a14]/60 rounded-lg p-3 border-l-2 border-${color}-400/40`}>
                        <div className="flex items-center gap-2 mb-2">
                          <span className={`text-sm font-bold text-${color}-400`}>{label}</span>
                          <span className="text-[10px] text-slate-500">{data.count} islem</span>
                        </div>
                        <div className="grid grid-cols-3 gap-2 text-xs">
                          <div>
                            <div className="text-[10px] text-slate-500 uppercase">Win Rate</div>
                            <div className={data.winRate >= 50 ? `text-emerald-400` : `text-red-400`}>{data.winRate}%</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-slate-500 uppercase">Toplam PnL</div>
                            <div className={pnlColor(data.totalPnl)}>{formatNum(data.totalPnl, 2, true)} USDT</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-slate-500 uppercase">Ort PnL</div>
                            <div className={pnlColor(data.avgPnl)}>{formatNum(data.avgPnl, 2, true)} USDT</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-slate-500 uppercase">Kazanan</div>
                            <div className="text-emerald-400">{data.wins}</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-slate-500 uppercase">Kaybeden</div>
                            <div className="text-red-400">{data.losses}</div>
                          </div>
                          <div>
                            <div className="text-[10px] text-slate-500 uppercase">Profit Factor</div>
                            <div className={data.pf >= 1 ? "text-emerald-400" : "text-red-400"}>{data.pf}</div>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </Section>
              );
            })()}

            {/* ── Exit Reason Summary ── */}
            {result!.trades.length > 0 && (() => {
              const reasons = [...new Set(result!.trades.map(t => t.exit_reason))].sort();
              const data = reasons.map(reason => {
                const trades = result!.trades.filter(t => t.exit_reason === reason);
                const totalPnl = trades.reduce((s, t) => s + t.pnl_usdt, 0);
                return {
                  reason,
                  count: trades.length,
                  pct: +(trades.length / result!.trades.length * 100).toFixed(1),
                  totalPnl: +totalPnl.toFixed(2),
                  avgPnl: +(totalPnl / trades.length).toFixed(2),
                  winRate: +(trades.filter(t => t.pnl_usdt > 0).length / trades.length * 100).toFixed(1),
                };
              });
              return (
                <Section title="Cikis Nedeni Analizi">
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                          <th className="text-left py-2 px-2">Neden</th>
                          <th className="text-right py-2 px-2">Islem</th>
                          <th className="text-right py-2 px-2">Oran</th>
                          <th className="text-right py-2 px-2">Win Rate</th>
                          <th className="text-right py-2 px-2">Toplam PnL</th>
                          <th className="text-right py-2 px-2">Ort PnL</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.map(r => (
                          <tr key={r.reason} className="border-b border-blue-500/[0.06]">
                            <td className="py-1.5 px-2">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                r.reason.startsWith("TP") ? "bg-emerald-400/10 text-emerald-300" :
                                r.reason === "SL" ? "bg-red-400/10 text-red-300" :
                                "bg-yellow-400/10 text-yellow-300"
                              }`}>{r.reason}</span>
                            </td>
                            <td className="py-1.5 px-2 text-right">{r.count}</td>
                            <td className="py-1.5 px-2 text-right text-slate-400">{r.pct}%</td>
                            <td className={`py-1.5 px-2 text-right ${r.winRate >= 50 ? "text-emerald-400" : "text-red-400"}`}>{r.winRate}%</td>
                            <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(r.totalPnl)}`}>{formatNum(r.totalPnl, 2, true)}</td>
                            <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(r.avgPnl)}`}>{formatNum(r.avgPnl, 2, true)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Section>
              );
            })()}

            {/* ── Equity Curve Chart ── */}
            {result!.equity_curve.length > 0 && (
              <Section title="Equity Curve">
                <ResponsiveContainer width="100%" height={300}>
                  <AreaChart data={result!.equity_curve}>
                    <defs>
                      <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#10b981" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="time" tickFormatter={fmtDate} tick={{ fontSize: 10, fill: "#64748b" }}
                      interval="preserveStartEnd" minTickGap={60} />
                    <YAxis tick={{ fontSize: 10, fill: "#64748b" }} domain={["auto", "auto"]}
                      tickFormatter={(v: number) => `${v.toFixed(0)}`} />
                    <Tooltip content={<ChartTooltip />} />
                    <Area type="monotone" dataKey="equity" name="Equity"
                      stroke="#10b981" fill="url(#eqGrad)" strokeWidth={1.5} dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Drawdown & Run-up Chart ── */}
            {result!.drawdown_curve.length > 0 && (
              <Section title="Run-up & Drawdown (%)">
                <ResponsiveContainer width="100%" height={250}>
                  <AreaChart data={result!.drawdown_curve}>
                    <defs>
                      <linearGradient id="ruGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#10b981" stopOpacity={0.25} />
                        <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#ef4444" stopOpacity={0} />
                        <stop offset="95%" stopColor="#ef4444" stopOpacity={0.25} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="time" tickFormatter={fmtDate} tick={{ fontSize: 10, fill: "#64748b" }}
                      interval="preserveStartEnd" minTickGap={60} />
                    <YAxis tick={{ fontSize: 10, fill: "#64748b" }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
                    <Tooltip content={<ChartTooltip />} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    <Area type="monotone" dataKey="runup_pct" name="Run-up %"
                      stroke="#10b981" fill="url(#ruGrad)" strokeWidth={1} dot={false} />
                    <Area type="monotone" dataKey="drawdown_pct" name="Drawdown %"
                      stroke="#ef4444" fill="url(#ddGrad)" strokeWidth={1} dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Monthly Returns Heatmap ── */}
            {monthlyReturns.length > 0 && (
              <Section title="Aylik Getiri">
                <div className="flex flex-wrap gap-2">
                  {monthlyReturns.map(({ month, pnl }) => {
                    const intensity = Math.min(Math.abs(pnl) / (Math.max(...monthlyReturns.map(r => Math.abs(r.pnl))) || 1), 1);
                    const bg = pnl >= 0
                      ? `rgba(16, 185, 129, ${0.1 + intensity * 0.5})`
                      : `rgba(239, 68, 68, ${0.1 + intensity * 0.5})`;
                    const [y, mo] = month.split("-");
                    return (
                      <div key={month} className="rounded-lg p-3 text-center min-w-[80px] border border-blue-500/[0.08]"
                        style={{ backgroundColor: bg }}>
                        <div className="text-[10px] text-slate-400 mb-1">{MONTHS_TR[+mo - 1]} {y}</div>
                        <div className={`text-sm font-mono font-semibold ${pnl >= 0 ? "text-emerald-300" : "text-red-300"}`}>
                          {formatNum(pnl, 2, true)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </Section>
            )}

            {/* ── PnL Distribution Histogram ── */}
            {pnlDistribution.length > 0 && (
              <Section title="PnL Dagilimi (Histogram)">
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={pnlDistribution}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="bin" tick={{ fontSize: 9, fill: "#64748b" }} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10, fill: "#64748b" }} />
                    <Tooltip content={({ active, payload }: any) => {
                      if (!active || !payload?.length) return null;
                      const d = payload[0].payload;
                      return (
                        <div className="bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-2 text-xs">
                          <p className="text-slate-400">Aralik: {d.bin} USDT</p>
                          <p className="text-slate-200">{d.count} islem</p>
                        </div>
                      );
                    }} />
                    <Bar dataKey="count" name="Islem Sayisi">
                      {pnlDistribution.map((d, i) => (
                        <Cell key={i} fill={d.binCenter >= 0 ? "#10b981" : "#ef4444"} fillOpacity={0.7} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Cumulative PnL by Symbol ── */}
            {cumulativePnlBySymbol && "points" in cumulativePnlBySymbol && cumulativePnlBySymbol.points.length > 0 && (
              <Section title="Kumulatif PnL (Parite Bazinda)">
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={cumulativePnlBySymbol.points}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="time" tickFormatter={fmtDate} tick={{ fontSize: 10, fill: "#64748b" }}
                      interval="preserveStartEnd" minTickGap={60} />
                    <YAxis tick={{ fontSize: 10, fill: "#64748b" }}
                      tickFormatter={(v: number) => `${v.toFixed(0)}`} />
                    <Tooltip content={<ChartTooltip />} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    {cumulativePnlBySymbol.symbols.map((sym, i) => {
                      const colors = ["#10b981", "#ef4444", "#3b82f6", "#f59e0b", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];
                      return (
                        <Line key={sym} type="monotone" dataKey={sym} name={sym}
                          stroke={colors[i % colors.length]} strokeWidth={1.5} dot={false} />
                      );
                    })}
                  </LineChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Trade Duration vs PnL Scatter ── */}
            {durationVsPnl.length > 0 && (
              <Section title="Islem Suresi vs PnL">
                <ResponsiveContainer width="100%" height={250}>
                  <ScatterChart>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="duration" name="Sure (dk)" tick={{ fontSize: 10, fill: "#64748b" }}
                      tickFormatter={(v: number) => `${v}dk`} />
                    <YAxis dataKey="pnl" name="PnL" tick={{ fontSize: 10, fill: "#64748b" }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}`} />
                    <ZAxis range={[20, 20]} />
                    <Tooltip content={({ active, payload }: any) => {
                      if (!active || !payload?.length) return null;
                      const d = payload[0].payload;
                      return (
                        <div className="bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-2 text-xs">
                          <p className="font-semibold">{d.symbol} ({d.side})</p>
                          <p className="text-slate-400">Sure: {d.duration} dk</p>
                          <p className={d.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>PnL: {formatNum(d.pnl, 4, true)} USDT</p>
                        </div>
                      );
                    }} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    <Scatter data={durationVsPnl.filter(d => d.pnl >= 0)} fill="#10b981" fillOpacity={0.6} />
                    <Scatter data={durationVsPnl.filter(d => d.pnl < 0)} fill="#ef4444" fillOpacity={0.6} />
                  </ScatterChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Rolling Win Rate ── */}
            {rollingWinRate.length > 0 && (
              <Section title={`Rolling Win Rate (Son ${Math.min(20, Math.floor((result?.trades.length || 0) / 2))} Islem)`}>
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={rollingWinRate}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="idx" tick={{ fontSize: 10, fill: "#64748b" }} />
                    <YAxis tick={{ fontSize: 10, fill: "#64748b" }} domain={[0, 100]}
                      tickFormatter={(v: number) => `${v}%`} />
                    <Tooltip content={({ active, payload }: any) => {
                      if (!active || !payload?.length) return null;
                      return (
                        <div className="bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-2 text-xs">
                          <p className="text-slate-400">Islem #{payload[0].payload.idx}</p>
                          <p className="text-sky-400">Win Rate: {payload[0].value}%</p>
                        </div>
                      );
                    }} />
                    <ReferenceLine y={50} stroke="#334155" strokeWidth={1} strokeDasharray="5 5" />
                    <Line type="monotone" dataKey="winRate" name="Win Rate %"
                      stroke="#38bdf8" strokeWidth={1.5} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Per-Trade PnL Chart ── */}
            {result!.trades.length > 0 && (() => {
              const chartTrades = downsample(result!.trades, 200);
              return (
              <Section title={`Islem Bazinda PnL (USDT) — ${result!.trades.length > 200 ? `${result!.trades.length} islemden 200 ornekleme` : `${result!.trades.length} islem`}`}>
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={chartTrades}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="id" tick={{ fontSize: 9, fill: "#64748b" }} />
                    <YAxis tick={{ fontSize: 10, fill: "#64748b" }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}`} />
                    <Tooltip content={({ active, payload }: any) => {
                      if (!active || !payload?.length) return null;
                      const t = payload[0].payload as BacktestTrade;
                      return (
                        <div className="bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-2 text-xs">
                          <p className="font-semibold mb-1">{t.symbol} #{t.id}</p>
                          <p className="text-slate-400">{t.side} — {t.exit_reason}</p>
                          <p>Entry: {formatNum(t.entry_price, 4)}</p>
                          <p>Exit: {formatNum(t.exit_price, 4)}</p>
                          <p className={t.pnl_usdt >= 0 ? "text-emerald-400" : "text-red-400"}>
                            PnL: {formatNum(t.pnl_usdt, 4, true)} USDT ({formatNum(t.pnl_pct, 2, true)}%)
                          </p>
                        </div>
                      );
                    }} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    <Bar dataKey="pnl_usdt" name="PnL">
                      {chartTrades.map((t, i) => (
                        <Cell key={i} fill={t.pnl_usdt >= 0 ? "#10b981" : "#ef4444"} fillOpacity={0.7} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </Section>
              );
            })()}

            {/* ── Trade Table (Paginated) ── */}
            {result!.trades.length > 0 && (() => {
              const uniqueSyms = [...new Set(result!.trades.map(t => t.symbol))].sort();
              const filtered = tradeFilter === "ALL" ? result!.trades : result!.trades.filter(t => t.symbol === tradeFilter);
              const totalPages = Math.ceil(filtered.length / TRADES_PER_PAGE);
              const safePage = Math.min(tradePage, totalPages - 1);
              const pageSlice = filtered.slice(safePage * TRADES_PER_PAGE, (safePage + 1) * TRADES_PER_PAGE);
              return (
              <Section title={`Islem Listesi (${filtered.length})`}>
                <div className="flex items-center gap-3 mb-3">
                  <select value={tradeFilter} onChange={(e) => { setTradeFilter(e.target.value); setTradePage(0); }}
                    className="bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-2 py-1 text-xs text-slate-200">
                    <option value="ALL">Tum Pairler</option>
                    {uniqueSyms.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                  {totalPages > 1 && (
                    <div className="flex items-center gap-1 ml-auto">
                      <button onClick={() => setTradePage(0)} disabled={safePage === 0}
                        className="px-2 py-1 text-[10px] rounded bg-blue-500/10 text-slate-300 hover:bg-blue-500/20 disabled:opacity-30">{"<<"}</button>
                      <button onClick={() => setTradePage(Math.max(0, safePage - 1))} disabled={safePage === 0}
                        className="px-2 py-1 text-[10px] rounded bg-blue-500/10 text-slate-300 hover:bg-blue-500/20 disabled:opacity-30">{"<"}</button>
                      <span className="text-[10px] text-slate-400 px-2">{safePage + 1} / {totalPages}</span>
                      <button onClick={() => setTradePage(Math.min(totalPages - 1, safePage + 1))} disabled={safePage >= totalPages - 1}
                        className="px-2 py-1 text-[10px] rounded bg-blue-500/10 text-slate-300 hover:bg-blue-500/20 disabled:opacity-30">{">"}</button>
                      <button onClick={() => setTradePage(totalPages - 1)} disabled={safePage >= totalPages - 1}
                        className="px-2 py-1 text-[10px] rounded bg-blue-500/10 text-slate-300 hover:bg-blue-500/20 disabled:opacity-30">{">>"}</button>
                    </div>
                  )}
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-[#0a1628]">
                      <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                        <th className="text-left py-2 px-2">#</th>
                        <th className="text-left py-2 px-2">Symbol</th>
                        <th className="text-center py-2">Side</th>
                        <th className="text-left py-2 px-2">Giris Zamani</th>
                        <th className="text-right py-2 px-2">Entry</th>
                        <th className="text-left py-2 px-2">Cikis Zamani</th>
                        <th className="text-right py-2 px-2">Exit</th>
                        <th className="text-center py-2 px-2">Reason</th>
                        <th className="text-right py-2 px-2">PnL (USDT)</th>
                        <th className="text-right py-2 px-2">PnL %</th>
                        <th className="text-right py-2 px-2">Fee</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageSlice.map((t) => (
                        <tr key={t.id} className="border-b border-blue-500/[0.06] hover:bg-blue-500/10">
                          <td className="py-1.5 px-2 text-slate-500">{t.id}</td>
                          <td className="py-1.5 px-2 font-semibold">{t.symbol}</td>
                          <td className="py-1.5 text-center">
                            <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                              t.side === "LONG" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"
                            }`}>{t.side}</span>
                          </td>
                          <td className="py-1.5 px-2 text-slate-400 font-mono whitespace-nowrap">
                            {t.entry_time ? fmtDateTime(t.entry_time) : "-"}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono">{formatNum(t.entry_price, 4)}</td>
                          <td className="py-1.5 px-2 text-slate-400 font-mono whitespace-nowrap">
                            {t.exit_time ? fmtDateTime(t.exit_time) : "-"}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono">{formatNum(t.exit_price, 4)}</td>
                          <td className="py-1.5 px-2 text-center">
                            <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                              t.exit_reason.startsWith("TP") ? "bg-emerald-400/10 text-emerald-300" :
                              t.exit_reason === "SL" ? "bg-red-400/10 text-red-300" :
                              "bg-yellow-400/10 text-yellow-300"
                            }`}>{t.exit_reason}</span>
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(t.pnl_usdt)}`}>
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
              </Section>
              );
            })()}

            {/* ── Day-of-Week Performance ── */}
            {result!.trades.length > 0 && (() => {
              const dayMap = new Map<number, { trades: number; pnl: number; wins: number; losses: number }>();
              for (let d = 0; d < 7; d++) dayMap.set(d, { trades: 0, pnl: 0, wins: 0, losses: 0 });

              for (const t of result!.trades) {
                if (!t.entry_time) continue;
                const day = new Date(t.entry_time).getUTCDay();
                const stat = dayMap.get(day)!;
                stat.trades++;
                stat.pnl += t.pnl_usdt;
                if (t.pnl_usdt > 0) stat.wins++;
                else stat.losses++;
              }

              const days = Array.from(dayMap.entries())
                .map(([d, s]) => ({ day: d, dayName: DAYS_TR[d], ...s }))
                .filter(d => d.trades > 0)
                .sort((a, b) => a.day - b.day);

              const bestDay = [...days].sort((a, b) => b.pnl - a.pnl)[0];
              const worstDay = [...days].sort((a, b) => a.pnl - b.pnl)[0];

              return (
                <Section title="Gun Bazli Performans (UTC)">
                  <div className="flex items-center gap-3 mb-3 text-[10px]">
                    {bestDay && (
                      <span className="text-emerald-400 bg-emerald-400/10 px-2 py-0.5 rounded">
                        En Iyi: {bestDay.dayName} ({formatNum(bestDay.pnl, 2, true)} USDT)
                      </span>
                    )}
                    {worstDay && (
                      <span className="text-red-400 bg-red-400/10 px-2 py-0.5 rounded">
                        En Kotu: {worstDay.dayName} ({formatNum(worstDay.pnl, 2, true)} USDT)
                      </span>
                    )}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                          <th className="text-left py-2 px-2">Gun</th>
                          <th className="text-right py-2 px-2">Islem</th>
                          <th className="text-right py-2 px-2">Kazanan</th>
                          <th className="text-right py-2 px-2">Kaybeden</th>
                          <th className="text-right py-2 px-2">Win Rate</th>
                          <th className="text-right py-2 px-2">Toplam PnL</th>
                          <th className="text-right py-2 px-2">Ort PnL</th>
                        </tr>
                      </thead>
                      <tbody>
                        {days.map(d => (
                          <tr key={d.day} className={`border-b border-blue-500/[0.06] ${
                            d.day === bestDay?.day ? "bg-emerald-400/5" :
                            d.day === worstDay?.day ? "bg-red-400/5" : ""
                          }`}>
                            <td className="py-1.5 px-2 font-semibold">{d.dayName}</td>
                            <td className="py-1.5 px-2 text-right">{d.trades}</td>
                            <td className="py-1.5 px-2 text-right text-emerald-400">{d.wins}</td>
                            <td className="py-1.5 px-2 text-right text-red-400">{d.losses}</td>
                            <td className={`py-1.5 px-2 text-right ${d.trades > 0 && (d.wins / d.trades * 100) >= 50 ? "text-emerald-400" : "text-red-400"}`}>
                              {d.trades > 0 ? formatNum(d.wins / d.trades * 100, 1) : "0.0"}%
                            </td>
                            <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(d.pnl)}`}>
                              {formatNum(d.pnl, 2, true)}
                            </td>
                            <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(d.pnl / d.trades)}`}>
                              {formatNum(d.pnl / d.trades, 2, true)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Section>
              );
            })()}

            {/* ── Saat Bazli Analiz (Parite Bazinda) ── */}
            {result!.trades.length > 0 && (() => {
              type HourStat = { hour: number; trades: number; pnl: number; wins: number; losses: number };
              type SymbolHourData = { symbol: string; hours: HourStat[]; bestHour: HourStat; worstHour: HourStat };

              const symbolsForAnalysis = [...new Set(result!.trades.map(t => t.symbol))].sort();
              const analysisData: SymbolHourData[] = symbolsForAnalysis.map(sym => {
                const symTrades = result!.trades.filter(t => t.symbol === sym && t.entry_time > 0);
                const hourMap = new Map<number, { pnl: number; trades: number; wins: number; losses: number }>();

                for (let h = 0; h < 24; h++) {
                  hourMap.set(h, { pnl: 0, trades: 0, wins: 0, losses: 0 });
                }

                for (const t of symTrades) {
                  const h = new Date(t.entry_time).getUTCHours();
                  const stat = hourMap.get(h)!;
                  stat.pnl += t.pnl_usdt;
                  stat.trades += 1;
                  if (t.pnl_usdt > 0) stat.wins += 1;
                  else stat.losses += 1;
                }

                const hours: HourStat[] = [];
                hourMap.forEach((v, h) => {
                  if (v.trades > 0) hours.push({ hour: h, ...v });
                });
                hours.sort((a, b) => b.pnl - a.pnl);

                const bestHour = hours[0] || { hour: 0, trades: 0, pnl: 0, wins: 0, losses: 0 };
                const worstHour = hours[hours.length - 1] || bestHour;

                return { symbol: sym, hours, bestHour, worstHour };
              });

              return (
              <Section title="Saat Bazli Performans Analizi (UTC)">
                <div className="space-y-4">
                  {analysisData.map(({ symbol, hours, bestHour, worstHour }) => (
                    <div key={symbol} className="bg-[#050a14]/60 rounded-lg p-3">
                      <div className="flex items-center gap-3 mb-2">
                        <span className="text-sm font-semibold text-slate-200">{symbol}</span>
                        <span className="text-[10px] text-emerald-400 bg-emerald-400/10 px-2 py-0.5 rounded">
                          En Iyi: {String(bestHour.hour).padStart(2, "0")}:00 ({formatNum(bestHour.pnl, 2, true)} USDT, {bestHour.trades} islem)
                        </span>
                        <span className="text-[10px] text-red-400 bg-red-400/10 px-2 py-0.5 rounded">
                          En Kotu: {String(worstHour.hour).padStart(2, "0")}:00 ({formatNum(worstHour.pnl, 2, true)} USDT, {worstHour.trades} islem)
                        </span>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                              <th className="text-left py-1.5 px-2">Saat (UTC)</th>
                              <th className="text-right py-1.5 px-2">Islem</th>
                              <th className="text-right py-1.5 px-2">Kazanan</th>
                              <th className="text-right py-1.5 px-2">Kaybeden</th>
                              <th className="text-right py-1.5 px-2">Win Rate</th>
                              <th className="text-right py-1.5 px-2">Toplam PnL</th>
                              <th className="text-right py-1.5 px-2">Ort PnL</th>
                            </tr>
                          </thead>
                          <tbody>
                            {hours.map((h) => (
                              <tr key={h.hour} className={`border-b border-blue-500/[0.06] ${
                                h.hour === bestHour.hour ? "bg-emerald-400/5" :
                                h.hour === worstHour.hour ? "bg-red-400/5" : ""
                              }`}>
                                <td className="py-1 px-2 font-mono text-slate-300">
                                  {String(h.hour).padStart(2, "0")}:00 - {String(h.hour).padStart(2, "0")}:59
                                </td>
                                <td className="py-1 px-2 text-right">{h.trades}</td>
                                <td className="py-1 px-2 text-right text-emerald-400">{h.wins}</td>
                                <td className="py-1 px-2 text-right text-red-400">{h.losses}</td>
                                <td className={`py-1 px-2 text-right ${h.trades > 0 && (h.wins / h.trades * 100) >= 50 ? "text-emerald-400" : "text-red-400"}`}>
                                  {h.trades > 0 ? formatNum(h.wins / h.trades * 100, 1) : "0.0"}%
                                </td>
                                <td className={`py-1 px-2 text-right font-mono ${pnlColor(h.pnl)}`}>
                                  {formatNum(h.pnl, 2, true)}
                                </td>
                                <td className={`py-1 px-2 text-right font-mono ${pnlColor(h.pnl / h.trades)}`}>
                                  {formatNum(h.pnl / h.trades, 2, true)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ))}
                </div>
              </Section>
              );
            })()}

          </>
        )}

        <div className="text-center text-[10px] text-slate-600 pb-4">
          Sigma Kapital Trading Technologies & Market Making Services
        </div>
      </div>
    </div>
  );
}
