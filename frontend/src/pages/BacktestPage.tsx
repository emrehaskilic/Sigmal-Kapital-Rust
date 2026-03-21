import { useState, useEffect, useRef, useMemo } from "react";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, BarChart, Bar, ReferenceLine, Cell,
  ScatterChart, Scatter, ZAxis, LineChart, Line,
} from "recharts";
import {
  fetchSymbols, fetchConfig, updateConfig, runBacktest,
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

/* ── heatmap color helper ── */
const heatColor = (value: number, max: number, type: "pnl" | "wr" = "pnl") => {
  if (type === "wr") {
    const intensity = Math.min(value / 100, 1);
    return value >= 50
      ? `rgba(20, 184, 166, ${intensity * 0.15})`
      : `rgba(244, 63, 94, ${(1 - intensity) * 0.15})`;
  }
  const absMax = Math.max(Math.abs(max), 1);
  const norm = Math.min(Math.abs(value) / absMax, 1);
  return value >= 0
    ? `rgba(20, 184, 166, ${norm * 0.15})`
    : `rgba(244, 63, 94, ${norm * 0.15})`;
};

/* ── Section wrapper ── */
const Section = ({ title, children, className = "" }: { title: string; children: React.ReactNode; className?: string }) => (
  <div className={`bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5 ${className}`}>
    <h2 className="text-xs font-semibold text-slate-400/80 mb-4 tracking-tight">{title}</h2>
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
  const [weeklyReset, setWeeklyReset] = useState(false);

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
    setProgress(0);
    setResult(null);
    setError(null);
    setBtStatus("starting");
    setTradeFilter("ALL");
    // Önce eski backtest'i resetle, sonra config gönder, sonra başlat
    await resetBacktest();
    await updateConfig(config);
    await runBacktest(selectedSymbols, lookbackDays, config, weeklyReset);
    // Poll'u backtest başladıktan SONRA aç
    setRunning(true);
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
      <div className="bg-[#0b1a2e] border border-slate-700/30 rounded-xl px-3.5 py-2.5 text-xs shadow-[0_4px_16px_rgba(0,0,0,0.4)]">
        <p className="text-slate-500/70 mb-1 text-[10px]">{typeof label === "number" && label > 1e9 ? fmtDate(label) : label}</p>
        {payload.map((p: any) => (
          <p key={p.dataKey} style={{ color: p.color }} className="font-mono">
            {p.name}: {formatNum(p.value, 2)}
            {p.dataKey.includes("pct") || p.dataKey === "winRate" ? "%" : p.dataKey === "equity" ? " USDT" : ""}
          </p>
        ))}
      </div>
    );
  };

  const cm: any = null; // comparison removed

  return (
    <div className="min-h-screen bg-[#040810] text-slate-200 p-4 md:p-6">
      <div className="max-w-7xl mx-auto space-y-5">

        {/* ── Header ── */}
        <div>
          <h1 className="text-xl font-bold text-slate-100">Backtest</h1>
          <span className="text-[11px] text-slate-500/60">Historical simulation using the same signal + risk engine</span>
        </div>

        {/* ── Config Panel ── */}
        {config && (
          <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5 space-y-4">
            {/* Trading */}
            <div>
              <h2 className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 font-semibold mb-2">Trading</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 text-xs">
                {[
                  { section: "trading", key: "initial_balance", label: "Ana Kasa (USDT)", step: "10" },
                  { section: "trading", key: "margin_per_trade", label: "Margin/Trade (USDT)", step: "10" },
                  { section: "trading", key: "leverage", label: "Kaldirac", step: "1" },
                ].map(({ section, key, label, step }) => (
                  <label key={key} className="space-y-1">
                    <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">{label}</span>
                    <input type="number" step={step}
                      value={(config as any)[section][key]}
                      onChange={(e) => handleConfigChange(section, key, +e.target.value)}
                      className="w-full bg-[#060e1a] border border-slate-700/25 rounded-xl px-2 py-1 text-sm font-mono font-bold text-slate-200 focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 focus:outline-none transition-all" />
                  </label>
                ))}
                <label className="space-y-1">
                  <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Islem Tipi</span>
                  <select value={config.trading.trade_type}
                    onChange={(e) => handleConfigChange("trading", "trade_type", e.target.value)}
                    className="w-full bg-[#060e1a] border border-slate-700/25 rounded-xl px-2 py-1 text-sm text-slate-200 focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 focus:outline-none transition-all">
                    {["BOTH", "LONG", "SHORT"].map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </label>
              </div>
            </div>

            {/* Adaptive PMax Info (read-only) */}
            <div>
              <div className="flex items-center gap-2 mb-1">
                <h2 className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 font-semibold">
                  Adaptive PMax — {config.strategy.timeframes[0]?.label || "3m"}
                </h2>
                <span className="text-[9px] px-2 py-0.5 rounded-full bg-teal-500/10 text-teal-400 font-mono">
                  Dinamik Parametreler
                </span>
              </div>
              <p className="text-[10px] text-slate-500/60">
                Parametreler volatiliteye gore otomatik ayarlanir (settings.yaml)
              </p>
            </div>
          </div>
        )}

        {/* ── Pair Selection + Controls ── */}
        <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5">
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <h2 className="text-xs font-semibold text-slate-400/80 tracking-tight">Pair Secimi</h2>
            <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">{selectedSymbols.length}/50</span>
            <div className="flex-1" />
            <label className="flex items-center gap-2 text-xs">
              <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Lookback</span>
              <input type="number" min={1} max={365} step={1}
                value={lookbackDays}
                onChange={(e) => setLookbackDays(+e.target.value)}
                className="w-16 bg-[#060e1a] border border-slate-700/25 rounded-xl px-2 py-1 text-sm font-mono font-bold text-slate-200 focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 focus:outline-none transition-all" />
              <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">gun</span>
            </label>
            <label className="flex items-center gap-2 text-xs">
              <input type="checkbox" checked={weeklyReset}
                onChange={(e) => setWeeklyReset(e.target.checked)}
                className="w-4 h-4 accent-teal-500 rounded" />
              <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Haftalik Reset</span>
            </label>
            {result && !running && (
              <button onClick={handleReset}
                className="px-4 py-1.5 rounded-xl text-xs font-semibold bg-slate-600/15 text-slate-300 border border-slate-600/20 hover:bg-rose-500/15 hover:text-rose-400 hover:border-rose-500/25 transition-all">
                Sifirla
              </button>
            )}
            <button onClick={handleRun}
              disabled={selectedSymbols.length === 0 || running || !!result}
              className="px-4 py-1.5 rounded-xl text-xs font-semibold bg-sky-500/15 text-sky-400 border border-sky-500/20 hover:bg-sky-500/25 transition-all disabled:opacity-30 disabled:cursor-not-allowed">
              {running ? "Calisiyor..." : "Backtest Baslat"}
            </button>
          </div>

          <div className="flex gap-3 mb-3">
            <div className="relative flex-1 max-w-xs">
              <input type="text" value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Pair ara... (BTCUSDT)"
                className="w-full bg-[#060e1a] border border-slate-700/25 rounded-xl px-3 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 transition-all" />
              {searchQuery && filteredSymbols.length > 0 && (
                <div className="absolute z-10 top-full left-0 right-0 mt-1 bg-[#0b1a2e] border border-slate-700/25 rounded-xl max-h-48 overflow-y-auto shadow-[0_4px_16px_rgba(0,0,0,0.4)]">
                  {filteredSymbols.slice(0, 20).map((s) => (
                    <button key={s} onClick={() => handleAddPair(s)}
                      className="w-full text-left px-3 py-1.5 text-xs text-slate-300 hover:bg-sky-500/10 transition-colors">
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
                  className="flex items-center gap-1 px-2.5 py-1 rounded-full bg-sky-500/10 text-xs text-slate-300 hover:bg-rose-500/20 hover:text-rose-400 transition-all">
                  {s} <span className="text-slate-500 hover:text-rose-400">&times;</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Progress Bar ── */}
        {running && (
          <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5">
            <div className="flex items-center justify-between text-xs mb-2">
              <span className="text-slate-400/80">{btStatus === "fetching" ? "Veri indiriliyor..." : btStatus === "computing" ? "Sinyaller hesaplaniyor..." : btStatus === "simulating" ? "Simulasyon calisiyor..." : "Metrikler hesaplaniyor..."}</span>
              <span className="text-slate-500/60 font-mono font-bold">{progress.toFixed(0)}%</span>
            </div>
            <div className="w-full h-2 bg-slate-700/30 rounded-2xl overflow-hidden">
              <div className="h-full bg-sky-400 transition-all duration-300 rounded-2xl"
                style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {/* ── Error ── */}
        {error && (
          <div className="bg-rose-500/10 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-4 text-xs text-rose-400">
            Backtest hatasi: {error}
          </div>
        )}

        {/* ══════════════════════════════════════════════════════════════════
            RESULTS
        ══════════════════════════════════════════════════════════════════ */}
        {m && (
          <>
            {/* ── Export Buttons (top-right) ── */}
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => result && exportBacktestPdf(result)}
                className="px-3 py-1.5 rounded-xl text-[10px] font-medium bg-slate-700/20 text-slate-400 hover:bg-slate-700/40 transition-all">
                PDF
              </button>
              <button onClick={handleExportCSV}
                className="px-3 py-1.5 rounded-xl text-[10px] font-medium bg-slate-700/20 text-slate-400 hover:bg-slate-700/40 transition-all">
                CSV
              </button>
              <button onClick={handleExportJSON}
                className="px-3 py-1.5 rounded-xl text-[10px] font-medium bg-slate-700/20 text-slate-400 hover:bg-slate-700/40 transition-all">
                JSON
              </button>
            </div>

            {/* ── Hero Metrics (bigger, with glow) ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5 bg-teal-500/5">
                <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 mb-1">Total PnL</div>
                <div className={`text-lg font-mono font-bold ${pnlColor(m.total_pnl)}`}>{formatNum(m.total_pnl, 2, true)} USDT</div>
                {cm && <div className="text-[9px] text-slate-500/60 mt-1">vs {formatNum(cm.total_pnl, 2, true)}</div>}
              </div>
              <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5 bg-teal-500/5">
                <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 mb-1">Win Rate</div>
                <div className={`text-lg font-mono font-bold ${m.win_rate >= 50 ? "text-teal-400" : "text-rose-400"}`}>{formatNum(m.win_rate, 1)}%</div>
                {cm && <div className="text-[9px] text-slate-500/60 mt-1">vs {formatNum(cm.win_rate, 1)}%</div>}
              </div>
              <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5 bg-rose-500/5">
                <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 mb-1">Max Drawdown</div>
                <div className="text-lg font-mono font-bold text-rose-400">{formatNum(m.max_drawdown_pct, 2)}%</div>
                {cm && <div className="text-[9px] text-slate-500/60 mt-1">vs {formatNum(cm.max_drawdown_pct, 2)}%</div>}
              </div>
              <div className="bg-[#0b1a2e]/80 rounded-2xl shadow-[0_2px_12px_rgba(0,0,0,0.25)] p-5 bg-teal-500/5">
                <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 mb-1">Profit Factor</div>
                <div className={`text-lg font-mono font-bold ${m.profit_factor >= 1 ? "text-teal-400" : "text-rose-400"}`}>{formatNum(m.profit_factor, 2)}</div>
                {cm && <div className="text-[9px] text-slate-500/60 mt-1">vs {formatNum(cm.profit_factor, 2)}</div>}
              </div>
            </div>

            {/* ── Summary Metrics Row 1 ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Total PnL %" value={`${formatNum(m.total_pnl_pct, 2, true)}%`} color={pnlColor(m.total_pnl_pct)}
                sub={cm ? `vs ${formatNum(cm.total_pnl_pct, 2, true)}%` : undefined} />
              <MetricTile label="Net (- Fees)" value={`${formatNum(m.total_pnl - m.total_fees, 2, true)} USDT`} color={pnlColor(m.total_pnl - m.total_fees)} />
              <MetricTile label="Bakiye" value={`${formatNum(m.current_balance, 2)} USDT`} color="text-sky-300" />
              <MetricTile label="Toplam Islem" value={m.total_trades}
                sub={cm ? `vs ${cm.total_trades}` : undefined} />
              <MetricTile label="Kazanan" value={m.winning_trades} color="text-teal-400" />
              <MetricTile label="Kaybeden" value={m.losing_trades} color="text-rose-400" />
            </div>

            {/* ── Summary Metrics Row 2 ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Avg Win" value={`${formatNum(m.avg_win, 2)} USDT`} color="text-teal-400" />
              <MetricTile label="Avg Loss" value={`${formatNum(m.avg_loss, 2)} USDT`} color="text-rose-400" />
              <MetricTile label="Gross Profit" value={`${formatNum(m.gross_profit, 2)} USDT`} color="text-teal-400" />
              <MetricTile label="Gross Loss" value={`${formatNum(m.gross_loss, 2)} USDT`} color="text-rose-400" />
              <MetricTile label="Max Run-up" value={`${formatNum(m.max_runup_pct, 2)}%`} color="text-teal-400" />
              <MetricTile label="Max DD (USDT)" value={`${formatNum(m.max_drawdown_usdt, 2)} USDT`} color="text-rose-400" />
            </div>

            {/* ── Summary Metrics Row 3 ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="Sharpe Ratio" value={formatNum(m.sharpe_ratio, 3)} color={m.sharpe_ratio >= 0 ? "text-teal-400" : "text-rose-400"} />
              <MetricTile label="Toplam Fee" value={`${formatNum(m.total_fees, 2)} USDT`} color="text-slate-400" />
              <MetricTile label="Sortino Ratio" value={formatNum(m.sortino_ratio, 3)} color={m.sortino_ratio >= 0 ? "text-teal-400" : "text-rose-400"} />
              <MetricTile label="Calmar Ratio" value={formatNum(m.calmar_ratio, 3)} color={m.calmar_ratio >= 0 ? "text-teal-400" : "text-rose-400"} />
              <MetricTile label="Recovery Factor" value={formatNum(m.recovery_factor, 2)} color={m.recovery_factor >= 1 ? "text-teal-400" : "text-rose-400"} />
              <MetricTile label="Expectancy" value={`${formatNum(m.expectancy, 2)} USDT`} color={pnlColor(m.expectancy)} />
            </div>

            {/* ── Summary Metrics Row 4 ── */}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <MetricTile label="En Iyi Islem" value={`${formatNum(m.best_trade_pnl, 2)} USDT`} color="text-teal-400" />
              <MetricTile label="En Kotu Islem" value={`${formatNum(m.worst_trade_pnl, 2)} USDT`} color="text-rose-400" />
              <MetricTile label="Seri Kazanma" value={m.max_consecutive_wins} color="text-teal-400" />
              <MetricTile label="Seri Kaybetme" value={m.max_consecutive_losses} color="text-rose-400" />
              <MetricTile label="Ort. Islem Suresi" value={`${m.avg_duration_min} dk`} color="text-sky-300" />
            </div>

            {/* ── Weekly Reset Summary ── */}
            {m.weekly_reset && (result as any).weekly_summary && (() => {
              const ws = (result as any).weekly_summary as any[];
              const pairSyms: string[] = m.symbols || [];
              const pairShorts = pairSyms.map((s: string) => s.replace("USDT", ""));
              const isMulti = pairSyms.length > 1;
              const subtitle = isMulti
                ? `${pairSyms.join(" + ")} | Kasa: $${formatNum(m.initial_balance, 0)} ($${formatNum(m.per_pair_balance, 0)}/pair)`
                : `${pairSyms[0] || selectedSymbols[0] || ""}`;
              return (
                <Section title={`Haftalik Sonuclar — ${m.winning_weeks}/${m.total_weeks} Karli (${m.weekly_win_rate}%) | Ort: $${formatNum(m.avg_weekly_profit, 0)} (${formatNum(m.avg_weekly_pnl_pct, 1, true)}%) | Toplam: $${formatNum(m.total_profit, 0)} | ${subtitle}`}>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-[9px] uppercase tracking-[0.1em] text-slate-500/60 border-b border-slate-700/20">
                          <th className="text-left py-2 px-2">W#</th>
                          <th className="text-left py-2 px-2">Donem</th>
                          {isMulti && pairShorts.map((s: string) => (
                            <th key={s} className="text-right py-2 px-2">{s}</th>
                          ))}
                          <th className="text-right py-2 px-2">Toplam</th>
                          <th className="text-right py-2 px-2">PnL%</th>
                          <th className="text-right py-2 px-2">WR</th>
                          <th className="text-right py-2 px-2">DD</th>
                          <th className="text-right py-2 px-2">Trades</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ws.map((w: any) => (
                          <tr key={w.week} className={`border-b border-slate-700/10 ${w.profit >= 0 ? "" : "bg-rose-500/5"}`}>
                            <td className="py-1.5 px-2 text-slate-400">W{w.week}</td>
                            <td className="py-1.5 px-2 text-slate-300 font-mono">{w.start} — {w.end}</td>
                            {isMulti && pairShorts.map((s: string) => {
                              const v = w[`${s}_profit`] ?? 0;
                              return (
                                <td key={s} className={`py-1.5 px-2 text-right font-mono ${v >= 0 ? "text-teal-400/80" : "text-rose-400/80"}`}>
                                  ${formatNum(v, 0, true)}
                                </td>
                              );
                            })}
                            <td className={`py-1.5 px-2 text-right font-mono font-bold ${w.profit >= 0 ? "text-teal-400" : "text-rose-400"}`}>${formatNum(w.profit, 0, true)}</td>
                            <td className={`py-1.5 px-2 text-right font-mono ${w.pnl_pct >= 0 ? "text-teal-400" : "text-rose-400"}`}>{formatNum(w.pnl_pct, 1, true)}%</td>
                            <td className={`py-1.5 px-2 text-right font-mono ${w.win_rate >= 90 ? "text-teal-400" : w.win_rate >= 80 ? "text-sky-400" : "text-amber-400"}`}>{formatNum(w.win_rate, 1)}%</td>
                            <td className={`py-1.5 px-2 text-right font-mono ${w.max_dd > 20 ? "text-rose-400" : w.max_dd > 10 ? "text-amber-400" : "text-slate-400"}`}>{formatNum(w.max_dd, 1)}%</td>
                            <td className="py-1.5 px-2 text-right font-mono text-slate-400">{w.trades}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Section>
              );
            })()}

            {/* ── Per-Symbol Metrics ── */}
            {result!.per_symbol && result!.per_symbol.length > 0 && (
              <Section title="Parite Bazinda Sonuclar">
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 border-b border-slate-700/20">
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
                        <tr key={ps.symbol} className="border-b border-slate-700/20 hover:bg-sky-500/5 transition-colors">
                          <td className="py-1.5 px-2 font-semibold">{ps.symbol}</td>
                          <td className="py-1.5 px-2 text-right text-sky-300 font-mono">{ps.total_trades}</td>
                          <td className="py-1.5 px-2 text-right text-teal-400 font-mono">{ps.winning_trades}</td>
                          <td className="py-1.5 px-2 text-right text-rose-400 font-mono">{ps.losing_trades}</td>
                          <td className={`py-1.5 px-2 text-right font-mono ${ps.win_rate >= 50 ? "text-teal-400" : "text-rose-400"}`}>
                            {formatNum(ps.win_rate, 1)}%
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono font-bold ${pnlColor(ps.total_pnl)}`}>
                            {formatNum(ps.total_pnl, 2, true)}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-slate-500/60">
                            {formatNum(ps.total_fees, 2)}
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono font-bold ${pnlColor(ps.total_pnl - ps.total_fees)}`}>
                            {formatNum(ps.total_pnl - ps.total_fees, 2, true)}
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono ${ps.profit_factor >= 1 ? "text-teal-400" : "text-rose-400"}`}>
                            {formatNum(ps.profit_factor, 2)}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-teal-400">
                            {formatNum(ps.avg_win, 2)}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-rose-400">
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
                      { label: "LONG", data: ld, color: "teal" },
                      { label: "SHORT", data: sd, color: "rose" },
                    ].map(({ label, data, color }) => (
                      <div key={label} className={`bg-[#060e1a]/60 rounded-xl p-4 border-l-2 ${color === "teal" ? "border-teal-400/40" : "border-rose-400/40"}`}>
                        <div className="flex items-center gap-2 mb-3">
                          <span className={`text-sm font-bold ${color === "teal" ? "text-teal-400" : "text-rose-400"}`}>{label}</span>
                          <span className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">{data.count} islem</span>
                        </div>
                        <div className="grid grid-cols-3 gap-2 text-xs">
                          <div>
                            <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Win Rate</div>
                            <div className={`font-mono font-bold ${data.winRate >= 50 ? "text-teal-400" : "text-rose-400"}`}>{data.winRate}%</div>
                          </div>
                          <div>
                            <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Toplam PnL</div>
                            <div className={`font-mono font-bold ${pnlColor(data.totalPnl)}`}>{formatNum(data.totalPnl, 2, true)} USDT</div>
                          </div>
                          <div>
                            <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Ort PnL</div>
                            <div className={`font-mono font-bold ${pnlColor(data.avgPnl)}`}>{formatNum(data.avgPnl, 2, true)} USDT</div>
                          </div>
                          <div>
                            <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Kazanan</div>
                            <div className="font-mono font-bold text-teal-400">{data.wins}</div>
                          </div>
                          <div>
                            <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Kaybeden</div>
                            <div className="font-mono font-bold text-rose-400">{data.losses}</div>
                          </div>
                          <div>
                            <div className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60">Profit Factor</div>
                            <div className={`font-mono font-bold ${data.pf >= 1 ? "text-teal-400" : "text-rose-400"}`}>{data.pf}</div>
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
                        <tr className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 border-b border-slate-700/20">
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
                          <tr key={r.reason} className="border-b border-slate-700/20 hover:bg-sky-500/5 transition-colors">
                            <td className="py-1.5 px-2">
                              <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${
                                r.reason.startsWith("TP") ? "bg-teal-400/10 text-teal-300" :
                                r.reason === "SL" ? "bg-rose-400/10 text-rose-300" :
                                "bg-yellow-400/10 text-yellow-300"
                              }`}>{r.reason}</span>
                            </td>
                            <td className="py-1.5 px-2 text-right font-mono text-sky-300">{r.count}</td>
                            <td className="py-1.5 px-2 text-right text-slate-400">{r.pct}%</td>
                            <td className={`py-1.5 px-2 text-right font-mono ${r.winRate >= 50 ? "text-teal-400" : "text-rose-400"}`}>{r.winRate}%</td>
                            <td className={`py-1.5 px-2 text-right font-mono font-bold ${pnlColor(r.totalPnl)}`}>{formatNum(r.totalPnl, 2, true)}</td>
                            <td className={`py-1.5 px-2 text-right font-mono font-bold ${pnlColor(r.avgPnl)}`}>{formatNum(r.avgPnl, 2, true)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Section>
              );
            })()}

            {/* ── Equity Curve Chart (full width at top) ── */}
            {result!.equity_curve.length > 0 && (
              <Section title="Equity Curve">
                <ResponsiveContainer width="100%" height={300}>
                  <AreaChart data={result!.equity_curve}>
                    <defs>
                      <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#2dd4bf" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#2dd4bf" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                    <XAxis dataKey="time" tickFormatter={fmtDate} tick={{ fontSize: 9, fill: "#64748b80" }}
                      interval="preserveStartEnd" minTickGap={60} />
                    <YAxis tick={{ fontSize: 9, fill: "#64748b80" }} domain={["auto", "auto"]}
                      tickFormatter={(v: number) => `${v.toFixed(0)}`} />
                    <Tooltip content={<ChartTooltip />} />
                    <Area type="monotone" dataKey="equity" name="Equity"
                      stroke="#2dd4bf" fill="url(#eqGrad)" strokeWidth={1.5} dot={false} />
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
                        <stop offset="5%" stopColor="#2dd4bf" stopOpacity={0.25} />
                        <stop offset="95%" stopColor="#2dd4bf" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#f43f5e" stopOpacity={0} />
                        <stop offset="95%" stopColor="#f43f5e" stopOpacity={0.25} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                    <XAxis dataKey="time" tickFormatter={fmtDate} tick={{ fontSize: 9, fill: "#64748b80" }}
                      interval="preserveStartEnd" minTickGap={60} />
                    <YAxis tick={{ fontSize: 9, fill: "#64748b80" }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
                    <Tooltip content={<ChartTooltip />} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    <Area type="monotone" dataKey="runup_pct" name="Run-up %"
                      stroke="#2dd4bf" fill="url(#ruGrad)" strokeWidth={1} dot={false} />
                    <Area type="monotone" dataKey="drawdown_pct" name="Drawdown %"
                      stroke="#f43f5e" fill="url(#ddGrad)" strokeWidth={1} dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── PnL Distribution, Monthly Returns, Rolling Win Rate (side by side) ── */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {/* PnL Distribution Histogram */}
              {pnlDistribution.length > 0 && (
                <Section title="PnL Dagilimi (Histogram)">
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart data={pnlDistribution}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                      <XAxis dataKey="bin" tick={{ fontSize: 9, fill: "#64748b80" }} interval="preserveStartEnd" />
                      <YAxis tick={{ fontSize: 9, fill: "#64748b80" }} />
                      <Tooltip content={({ active, payload }: any) => {
                        if (!active || !payload?.length) return null;
                        const d = payload[0].payload;
                        return (
                          <div className="bg-[#0b1a2e] border border-slate-700/30 rounded-xl px-3.5 py-2.5 text-xs shadow-[0_4px_16px_rgba(0,0,0,0.4)]">
                            <p className="text-slate-500/70 text-[10px]">Aralik: {d.bin} USDT</p>
                            <p className="text-slate-200 font-mono">{d.count} islem</p>
                          </div>
                        );
                      }} />
                      <Bar dataKey="count" name="Islem Sayisi">
                        {pnlDistribution.map((d, i) => (
                          <Cell key={i} fill={d.binCenter >= 0 ? "#2dd4bf" : "#f43f5e"} fillOpacity={0.7} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </Section>
              )}

              {/* Monthly Returns Heatmap */}
              {monthlyReturns.length > 0 && (
                <Section title="Aylik Getiri">
                  <div className="flex flex-wrap gap-2">
                    {monthlyReturns.map(({ month, pnl }) => {
                      const intensity = Math.min(Math.abs(pnl) / (Math.max(...monthlyReturns.map(r => Math.abs(r.pnl))) || 1), 1);
                      const bg = pnl >= 0
                        ? `rgba(20, 184, 166, ${0.1 + intensity * 0.5})`
                        : `rgba(244, 63, 94, ${0.1 + intensity * 0.5})`;
                      const [y, mo] = month.split("-");
                      return (
                        <div key={month} className="rounded-xl p-3 text-center min-w-[80px]"
                          style={{ backgroundColor: bg }}>
                          <div className="text-[9px] uppercase tracking-[0.12em] text-slate-400/80 mb-1">{MONTHS_TR[+mo - 1]} {y}</div>
                          <div className={`text-sm font-mono font-bold ${pnl >= 0 ? "text-teal-300" : "text-rose-300"}`}>
                            {formatNum(pnl, 2, true)}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </Section>
              )}

              {/* Rolling Win Rate */}
              {rollingWinRate.length > 0 && (
                <Section title={`Rolling Win Rate (${Math.min(20, Math.floor((result?.trades.length || 0) / 2))})`}>
                  <ResponsiveContainer width="100%" height={200}>
                    <LineChart data={rollingWinRate}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                      <XAxis dataKey="idx" tick={{ fontSize: 9, fill: "#64748b80" }} />
                      <YAxis tick={{ fontSize: 9, fill: "#64748b80" }} domain={[0, 100]}
                        tickFormatter={(v: number) => `${v}%`} />
                      <Tooltip content={({ active, payload }: any) => {
                        if (!active || !payload?.length) return null;
                        return (
                          <div className="bg-[#0b1a2e] border border-slate-700/30 rounded-xl px-3.5 py-2.5 text-xs shadow-[0_4px_16px_rgba(0,0,0,0.4)]">
                            <p className="text-slate-500/70 text-[10px]">Islem #{payload[0].payload.idx}</p>
                            <p className="text-sky-300 font-mono">Win Rate: {payload[0].value}%</p>
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
            </div>

            {/* ── Duration vs PnL Scatter ── */}
            {durationVsPnl.length > 0 && (
              <Section title="Islem Suresi vs PnL">
                <ResponsiveContainer width="100%" height={250}>
                  <ScatterChart>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                    <XAxis dataKey="duration" name="Sure (dk)" tick={{ fontSize: 9, fill: "#64748b80" }}
                      tickFormatter={(v: number) => `${v}dk`} />
                    <YAxis dataKey="pnl" name="PnL" tick={{ fontSize: 9, fill: "#64748b80" }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}`} />
                    <ZAxis range={[20, 20]} />
                    <Tooltip content={({ active, payload }: any) => {
                      if (!active || !payload?.length) return null;
                      const d = payload[0].payload;
                      return (
                        <div className="bg-[#0b1a2e] border border-slate-700/30 rounded-xl px-3.5 py-2.5 text-xs shadow-[0_4px_16px_rgba(0,0,0,0.4)]">
                          <p className="font-semibold">{d.symbol} ({d.side})</p>
                          <p className="text-slate-500/70 text-[10px]">Sure: {d.duration} dk</p>
                          <p className={`font-mono ${d.pnl >= 0 ? "text-teal-400" : "text-rose-400"}`}>PnL: {formatNum(d.pnl, 4, true)} USDT</p>
                        </div>
                      );
                    }} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    <Scatter data={durationVsPnl.filter(d => d.pnl >= 0)} fill="#2dd4bf" fillOpacity={0.6} />
                    <Scatter data={durationVsPnl.filter(d => d.pnl < 0)} fill="#f43f5e" fillOpacity={0.6} />
                  </ScatterChart>
                </ResponsiveContainer>
              </Section>
            )}

            {/* ── Cumulative PnL by Symbol ── */}
            {cumulativePnlBySymbol && "points" in cumulativePnlBySymbol && cumulativePnlBySymbol.points.length > 0 && (
              <Section title="Kumulatif PnL (Parite Bazinda)">
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={cumulativePnlBySymbol.points}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                    <XAxis dataKey="time" tickFormatter={fmtDate} tick={{ fontSize: 9, fill: "#64748b80" }}
                      interval="preserveStartEnd" minTickGap={60} />
                    <YAxis tick={{ fontSize: 9, fill: "#64748b80" }}
                      tickFormatter={(v: number) => `${v.toFixed(0)}`} />
                    <Tooltip content={<ChartTooltip />} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    {cumulativePnlBySymbol.symbols.map((sym, i) => {
                      const colors = ["#2dd4bf", "#f43f5e", "#38bdf8", "#f59e0b", "#8b5cf6", "#ec4899", "#06b6d4", "#84cc16"];
                      return (
                        <Line key={sym} type="monotone" dataKey={sym} name={sym}
                          stroke={colors[i % colors.length]} strokeWidth={1.5} dot={false} />
                      );
                    })}
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
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b18" />
                    <XAxis dataKey="id" tick={{ fontSize: 9, fill: "#64748b80" }} />
                    <YAxis tick={{ fontSize: 9, fill: "#64748b80" }}
                      tickFormatter={(v: number) => `${v.toFixed(1)}`} />
                    <Tooltip content={({ active, payload }: any) => {
                      if (!active || !payload?.length) return null;
                      const t = payload[0].payload as BacktestTrade;
                      return (
                        <div className="bg-[#0b1a2e] border border-slate-700/30 rounded-xl px-3.5 py-2.5 text-xs shadow-[0_4px_16px_rgba(0,0,0,0.4)]">
                          <p className="font-semibold mb-1">{t.symbol} #{t.id}</p>
                          <p className="text-slate-500/70 text-[10px]">{t.side} — {t.exit_reason}</p>
                          <p className="font-mono text-sky-300">Entry: {formatNum(t.entry_price, 4)}</p>
                          <p className="font-mono text-sky-300">Exit: {formatNum(t.exit_price, 4)}</p>
                          <p className={`font-mono ${t.pnl_usdt >= 0 ? "text-teal-400" : "text-rose-400"}`}>
                            PnL: {formatNum(t.pnl_usdt, 4, true)} USDT ({formatNum(t.pnl_pct, 2, true)}%)
                          </p>
                        </div>
                      );
                    }} />
                    <ReferenceLine y={0} stroke="#334155" strokeWidth={1} />
                    <Bar dataKey="pnl_usdt" name="PnL">
                      {chartTrades.map((t, i) => (
                        <Cell key={i} fill={t.pnl_usdt >= 0 ? "#2dd4bf" : "#f43f5e"} fillOpacity={0.7} />
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
                    className="bg-[#060e1a] border border-slate-700/25 rounded-xl px-2 py-1 text-xs text-slate-200 focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 focus:outline-none transition-all">
                    <option value="ALL">Tum Pairler</option>
                    {uniqueSyms.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                  {totalPages > 1 && (
                    <div className="flex items-center gap-1 ml-auto">
                      <button onClick={() => setTradePage(0)} disabled={safePage === 0}
                        className="px-2 py-1 text-[10px] rounded-xl bg-slate-700/20 text-slate-300 hover:bg-slate-700/40 disabled:opacity-30 transition-all">{"<<"}</button>
                      <button onClick={() => setTradePage(Math.max(0, safePage - 1))} disabled={safePage === 0}
                        className="px-2 py-1 text-[10px] rounded-xl bg-slate-700/20 text-slate-300 hover:bg-slate-700/40 disabled:opacity-30 transition-all">{"<"}</button>
                      <span className="text-[10px] text-slate-500/60 px-2 font-mono">{safePage + 1} / {totalPages}</span>
                      <button onClick={() => setTradePage(Math.min(totalPages - 1, safePage + 1))} disabled={safePage >= totalPages - 1}
                        className="px-2 py-1 text-[10px] rounded-xl bg-slate-700/20 text-slate-300 hover:bg-slate-700/40 disabled:opacity-30 transition-all">{">"}</button>
                      <button onClick={() => setTradePage(totalPages - 1)} disabled={safePage >= totalPages - 1}
                        className="px-2 py-1 text-[10px] rounded-xl bg-slate-700/20 text-slate-300 hover:bg-slate-700/40 disabled:opacity-30 transition-all">{">>"}</button>
                    </div>
                  )}
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-[#0b1a2e]/80">
                      <tr className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 border-b border-slate-700/20">
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
                        <tr key={t.id} className="border-b border-slate-700/20 hover:bg-sky-500/5 transition-colors">
                          <td className="py-1.5 px-2 text-slate-500/60 font-mono">{t.id}</td>
                          <td className="py-1.5 px-2 font-semibold">{t.symbol}</td>
                          <td className="py-1.5 text-center">
                            <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${
                              t.side === "LONG" ? "bg-teal-400/15 text-teal-400" : "bg-rose-400/15 text-rose-400"
                            }`}>{t.side}</span>
                          </td>
                          <td className="py-1.5 px-2 text-slate-400 font-mono whitespace-nowrap">
                            {t.entry_time ? fmtDateTime(t.entry_time) : "-"}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-sky-300">{formatNum(t.entry_price, 4)}</td>
                          <td className="py-1.5 px-2 text-slate-400 font-mono whitespace-nowrap">
                            {t.exit_time ? fmtDateTime(t.exit_time) : "-"}
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-sky-300">{formatNum(t.exit_price, 4)}</td>
                          <td className="py-1.5 px-2 text-center">
                            <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${
                              t.exit_reason.startsWith("TP") ? "bg-teal-400/10 text-teal-300" :
                              t.exit_reason === "SL" ? "bg-rose-400/10 text-rose-300" :
                              "bg-yellow-400/10 text-yellow-300"
                            }`}>{t.exit_reason}</span>
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono font-bold ${pnlColor(t.pnl_usdt)}`}>
                            {formatNum(t.pnl_usdt, 4, true)}
                          </td>
                          <td className={`py-1.5 px-2 text-right font-mono font-bold ${pnlColor(t.pnl_pct)}`}>
                            {formatNum(t.pnl_pct, 2, true)}%
                          </td>
                          <td className="py-1.5 px-2 text-right font-mono text-slate-500/60">
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
              const maxDayPnl = Math.max(...days.map(d => Math.abs(d.pnl)), 1);

              return (
                <Section title="Gun Bazli Performans (UTC)">
                  <div className="flex items-center gap-3 mb-3 text-[10px]">
                    {bestDay && (
                      <span className="text-teal-400 bg-teal-400/10 px-2.5 py-0.5 rounded-full">
                        En Iyi: {bestDay.dayName} ({formatNum(bestDay.pnl, 2, true)} USDT)
                      </span>
                    )}
                    {worstDay && (
                      <span className="text-rose-400 bg-rose-400/10 px-2.5 py-0.5 rounded-full">
                        En Kotu: {worstDay.dayName} ({formatNum(worstDay.pnl, 2, true)} USDT)
                      </span>
                    )}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 border-b border-slate-700/20">
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
                        {days.map(d => {
                          const wr = d.trades > 0 ? d.wins / d.trades * 100 : 0;
                          return (
                          <tr key={d.day} className="border-b border-slate-700/20 hover:bg-sky-500/5 transition-colors">
                            <td className="py-1.5 px-2 font-semibold">{d.dayName}</td>
                            <td className="py-1.5 px-2 text-right font-mono text-sky-300">{d.trades}</td>
                            <td className="py-1.5 px-2 text-right font-mono text-teal-400">{d.wins}</td>
                            <td className="py-1.5 px-2 text-right font-mono text-rose-400">{d.losses}</td>
                            <td className="py-1.5 px-2 text-right font-mono"
                              style={{ backgroundColor: heatColor(wr, 100, "wr") }}>
                              <span className={wr >= 50 ? "text-teal-400" : "text-rose-400"}>
                                {d.trades > 0 ? formatNum(wr, 1) : "0.0"}%
                              </span>
                            </td>
                            <td className="py-1.5 px-2 text-right font-mono font-bold"
                              style={{ backgroundColor: heatColor(d.pnl, maxDayPnl) }}>
                              <span className={pnlColor(d.pnl)}>{formatNum(d.pnl, 2, true)}</span>
                            </td>
                            <td className="py-1.5 px-2 text-right font-mono font-bold"
                              style={{ backgroundColor: heatColor(d.pnl / d.trades, maxDayPnl) }}>
                              <span className={pnlColor(d.pnl / d.trades)}>{formatNum(d.pnl / d.trades, 2, true)}</span>
                            </td>
                          </tr>
                          );
                        })}
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
                  {analysisData.map(({ symbol, hours, bestHour, worstHour }) => {
                    const maxHourPnl = Math.max(...hours.map(h => Math.abs(h.pnl)), 1);
                    return (
                    <div key={symbol} className="bg-[#060e1a]/60 rounded-xl p-4">
                      <div className="flex items-center gap-3 mb-3">
                        <span className="text-sm font-semibold text-slate-200">{symbol}</span>
                        <span className="text-[10px] text-teal-400 bg-teal-400/10 px-2.5 py-0.5 rounded-full">
                          En Iyi: {String(bestHour.hour).padStart(2, "0")}:00 ({formatNum(bestHour.pnl, 2, true)} USDT, {bestHour.trades} islem)
                        </span>
                        <span className="text-[10px] text-rose-400 bg-rose-400/10 px-2.5 py-0.5 rounded-full">
                          En Kotu: {String(worstHour.hour).padStart(2, "0")}:00 ({formatNum(worstHour.pnl, 2, true)} USDT, {worstHour.trades} islem)
                        </span>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="text-[9px] uppercase tracking-[0.12em] text-slate-500/60 border-b border-slate-700/20">
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
                            {hours.map((h) => {
                              const wr = h.trades > 0 ? h.wins / h.trades * 100 : 0;
                              return (
                              <tr key={h.hour} className="border-b border-slate-700/20 hover:bg-sky-500/5 transition-colors">
                                <td className="py-1 px-2 font-mono text-slate-300">
                                  {String(h.hour).padStart(2, "0")}:00 - {String(h.hour).padStart(2, "0")}:59
                                </td>
                                <td className="py-1 px-2 text-right font-mono text-sky-300">{h.trades}</td>
                                <td className="py-1 px-2 text-right font-mono text-teal-400">{h.wins}</td>
                                <td className="py-1 px-2 text-right font-mono text-rose-400">{h.losses}</td>
                                <td className="py-1 px-2 text-right font-mono"
                                  style={{ backgroundColor: heatColor(wr, 100, "wr") }}>
                                  <span className={wr >= 50 ? "text-teal-400" : "text-rose-400"}>
                                    {h.trades > 0 ? formatNum(wr, 1) : "0.0"}%
                                  </span>
                                </td>
                                <td className="py-1 px-2 text-right font-mono font-bold"
                                  style={{ backgroundColor: heatColor(h.pnl, maxHourPnl) }}>
                                  <span className={pnlColor(h.pnl)}>{formatNum(h.pnl, 2, true)}</span>
                                </td>
                                <td className="py-1 px-2 text-right font-mono font-bold"
                                  style={{ backgroundColor: heatColor(h.pnl / h.trades, maxHourPnl) }}>
                                  <span className={pnlColor(h.pnl / h.trades)}>{formatNum(h.pnl / h.trades, 2, true)}</span>
                                </td>
                              </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    </div>
                    );
                  })}
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
