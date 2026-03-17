import { useState, useEffect, useRef } from "react";
import { fetchSymbols, fetchStatus, startBot, stopBot, fetchConfig, updateConfig } from "./api";
import type { StatusResponse, Config, TimeframeConfig } from "./types";
import { MetricTile } from "./components/MetricTile";
import { Badge } from "./components/Badge";
import { PositionTable } from "./components/PositionTable";
import { PairGrid } from "./components/PairGrid";
import { TradeTable } from "./components/TradeTable";
import { PMaxChart } from "./components/PMaxChart";
import { formatNum, pnlColor } from "./utils";

export default function App() {
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [activeSymbols, setActiveSymbols] = useState<string[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [botRunning, setBotRunning] = useState(false);
  const [loading, setLoading] = useState(false);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    fetchSymbols().then((d) => setAllSymbols(d.symbols));
    fetchConfig().then((c) => setConfig(c));
  }, []);

  useEffect(() => {
    if (botRunning) {
      const poll = () => {
        fetchStatus().then(setStatus).catch(console.error);
      };
      poll();
      pollRef.current = window.setInterval(poll, 1000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [botRunning]);

  const handleAddPair = (sym: string) => {
    if (activeSymbols.length >= 50) return;
    setActiveSymbols((prev) => [...prev, sym]);
    setSearchQuery("");
  };

  const handleRemovePair = (sym: string) => {
    setActiveSymbols((prev) => prev.filter((s) => s !== sym));
  };

  const handleStart = async () => {
    if (activeSymbols.length === 0) return;
    setLoading(true);
    if (config) await updateConfig(config);
    await startBot(activeSymbols);
    setBotRunning(true);
    setLoading(false);
  };

  const handleStop = async () => {
    await stopBot();
    setBotRunning(false);
    setStatus(null);
  };

  const handleConfigChange = (section: string, key: string, value: number | string | boolean) => {
    if (!config) return;
    setConfig({
      ...config,
      [section]: { ...(config as any)[section], [key]: value },
    });
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
    setConfig({
      ...config,
      strategy: { ...config.strategy, timeframes: tfs },
    });
  };

  const filteredSymbols = allSymbols
    .filter((s) => !activeSymbols.includes(s))
    .filter((s) => s.toLowerCase().includes(searchQuery.toLowerCase()));

  const priceSource = status?.price_source ?? "none";
  const wsStatus = priceSource === "websocket" ? "WS LIVE" : status?.ws_connected ? "REST" : botRunning ? "CONNECTING" : "DISCONNECTED";
  const stats = status?.stats;
  const totals = status?.totals;
  const fees = status?.fees;

  return (
    <div className="min-h-screen bg-[#050a14] text-slate-200 p-4 md:p-6">
      <div className="max-w-7xl mx-auto space-y-5">

        {/* ── Header ── */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-slate-100">Sigma Kapital</h1>
            <span className="text-[11px] text-slate-500">Dry-Run Simulation Engine v0.1.0</span>
          </div>
          <div className="flex items-center gap-3">
            <Badge status={wsStatus as any} label={wsStatus} />
            {botRunning ? (
              <button onClick={handleStop}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-red-500/15 text-red-400 border border-red-500/25 hover:bg-red-500/25 transition-colors">
                Botu Durdur
              </button>
            ) : (
              <button onClick={handleStart}
                disabled={activeSymbols.length === 0 || loading}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-sky-500/15 text-sky-400 border border-sky-500/25 hover:bg-sky-500/25 transition-colors disabled:opacity-30 disabled:cursor-not-allowed">
                {loading ? "Tarama yapiliyor..." : "Botu Baslat"}
              </button>
            )}
          </div>
        </div>

        {/* ── Pair Selection ── */}
        <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
          <div className="flex items-center gap-3 mb-3">
            <h2 className="text-sm font-semibold text-slate-300">Pair Secimi</h2>
            <span className="text-[10px] text-slate-500">{activeSymbols.length}/50</span>
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
          {activeSymbols.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {activeSymbols.map((s) => (
                <button key={s} onClick={() => handleRemovePair(s)}
                  className="flex items-center gap-1 px-2 py-1 rounded bg-blue-500/10 text-xs text-slate-300 hover:bg-red-500/20 hover:text-red-400 transition-colors">
                  {s} <span className="text-slate-500 hover:text-red-400">×</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Config Panel ── */}
        {!botRunning && config && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4 space-y-3">
            {/* ── Trading Settings ── */}
            <div className="bg-[#050a14]/60 rounded-lg p-3">
              <h2 className="text-[10px] font-semibold text-sky-400/70 uppercase tracking-wider mb-2">Trading</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                {[
                  { section: "trading", key: "initial_balance", label: "Ana Kasa (USDT)", step: "10" },
                  { section: "trading", key: "margin_per_trade", label: "Base Margin/Trade", step: "10" },
                  { section: "trading", key: "leverage", label: "Kaldirac", step: "1" },
                ].map(({ section, key, label, step }) => (
                  <label key={key} className="space-y-0.5">
                    <span className="text-slate-500 text-[10px] uppercase">{label}</span>
                    <input type="number" step={step}
                      value={(config as any)[section][key]}
                      onChange={(e) => handleConfigChange(section, key, +e.target.value)}
                      className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-2 py-1 text-sm font-mono text-slate-200" />
                  </label>
                ))}
                <label className="space-y-0.5">
                  <span className="text-slate-500 text-[10px] uppercase">Islem Tipi</span>
                  <select value={config.trading.trade_type}
                    onChange={(e) => handleConfigChange("trading", "trade_type", e.target.value)}
                    className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-2 py-1 text-sm text-slate-200">
                    {["BOTH","LONG","SHORT"].map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </label>
              </div>
            </div>

            {/* ── Adaptive PMax Info (read-only) ── */}
            <div className="bg-[#050a14]/60 rounded-lg p-3">
              <div className="flex items-center gap-2">
                <h2 className="text-[10px] font-semibold text-sky-400/70 uppercase tracking-wider">
                  Adaptive PMax — {config.strategy.timeframes[0]?.label || "3m"}
                </h2>
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 font-mono">
                  Dinamik Parametreler
                </span>
              </div>
              <p className="text-[10px] text-slate-500 mt-1">
                Parametreler volatiliteye gore otomatik ayarlanir (settings.yaml)
              </p>
            </div>

          </div>
        )}

        {/* ── Summary Metrics ── */}
        {stats && (
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
            <MetricTile label="Bakiye" value={`${formatNum(stats.current_balance, 2)} USDT`} color="text-sky-400" />
            <MetricTile label="Kaldirac" value={`${stats.leverage}x`} />
            <MetricTile label="Aktif Pair" value={status?.active_symbols.length || 0} />
            <MetricTile label="Toplam Islem" value={stats.total_trades} />
            <MetricTile label="Win Rate" value={`${formatNum(stats.win_rate, 1)}%`}
              color={stats.win_rate >= 50 ? "text-emerald-400" : "text-red-400"} />
            <MetricTile label="Fiyat Kaynak" value={wsStatus}
              color={wsStatus === "WS LIVE" ? "text-emerald-400" : wsStatus === "REST" ? "text-yellow-400" : "text-slate-500"} />
          </div>
        )}

        {/* ── PnL Summary ── */}
        {totals && (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <MetricTile label="Unrealized PnL" value={`${formatNum(totals.unrealized_pnl, 4, true)} USDT`} color={pnlColor(totals.unrealized_pnl)} />
            <MetricTile label="Realized PnL" value={`${formatNum(totals.realized_pnl, 4, true)} USDT`} color={pnlColor(totals.realized_pnl)} />
            <MetricTile label="Total PnL" value={`${formatNum(totals.total_pnl, 4, true)} USDT`} color={pnlColor(totals.total_pnl)} />
            <MetricTile label="Net PnL (- Fees)" value={`${formatNum(totals.net_pnl, 4, true)} USDT`} color={pnlColor(totals.net_pnl)} />
            <MetricTile label="Total PnL %" value={`${formatNum(
              stats!.initial_balance > 0
                ? ((totals.total_pnl - totals.total_fees) / stats!.initial_balance) * 100
                : 0, 2, true)}%`} color={pnlColor(totals.net_pnl)} />
          </div>
        )}

        {/* ── Fee Breakdown ── */}
        {fees && (
          <div className="flex gap-4 text-[11px] text-slate-500 px-1">
            <span>Maker Fee: {formatNum(fees.maker, 4)} USDT</span>
            <span>Taker Fee: {formatNum(fees.taker, 4)} USDT</span>
            <span>Total Fees: {formatNum(fees.total, 4)} USDT</span>
          </div>
        )}

        {/* ── Pair Grid ── */}
        {status?.pair_summaries && Object.keys(status.pair_summaries).length > 0 && (
          <div>
            <h2 className="text-sm font-semibold text-slate-300 mb-3">Pair Durumlari</h2>
            <PairGrid pairs={status.pair_summaries} />
          </div>
        )}

        {/* ── PMax Chart ── */}
        {botRunning && activeSymbols.length > 0 && (
          <PMaxChart key={activeSymbols[0]} symbol={activeSymbols[0]} botRunning={botRunning} />
        )}

        {/* ── Open Positions ── */}
        {status?.positions && status.positions.length > 0 && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
            <h2 className="text-sm font-semibold text-slate-300 mb-3">Acik Pozisyonlar</h2>
            <PositionTable positions={status.positions} />
          </div>
        )}

        {/* ── Trade History ── */}
        <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
          <h2 className="text-sm font-semibold text-slate-300 mb-3">Islem Gecmisi</h2>
          <TradeTable trades={status?.trade_log || []} />
        </div>

        {/* ── Signal Log ── */}
        {status?.signal_log && status.signal_log.length > 0 && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
            <h2 className="text-sm font-semibold text-slate-300 mb-3">Sinyal Gecmisi</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                    <th className="text-left py-2 px-2">Zaman</th>
                    <th className="text-left py-2 px-2">Symbol</th>
                    <th className="text-center py-2">Side</th>
                    <th className="text-right py-2 px-2">Fiyat</th>
                    <th className="text-right py-2 px-2">RSI</th>
                    <th className="text-left py-2 px-2">Kaynak</th>
                  </tr>
                </thead>
                <tbody>
                  {status.signal_log.map((s, i) => (
                    <tr key={i} className="border-b border-blue-500/[0.06]">
                      <td className="py-1.5 px-2 text-slate-400">{s.time}</td>
                      <td className="py-1.5 px-2 font-semibold">{s.symbol}</td>
                      <td className="py-1.5 text-center">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                          s.side === "LONG" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"
                        }`}>{s.side}</span>
                      </td>
                      <td className="py-1.5 px-2 text-right font-mono">{formatNum(s.price, 4)}</td>
                      <td className="py-1.5 px-2 text-right font-mono text-slate-400">{s.rsi}</td>
                      <td className="py-1.5 px-2 text-slate-500">{s.source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="text-center text-[10px] text-slate-600 pb-4">
          Sigma Kapital Trading Technologies & Market Making Services
        </div>
      </div>
    </div>
  );
}
