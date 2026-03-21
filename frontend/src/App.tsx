import { useState, useEffect, useRef } from "react";
import { fetchSymbols, fetchStatus, startBot, stopBot, fetchConfig, updateConfig } from "./api";
import type { StatusResponse, Config, TimeframeConfig } from "./types";
import { MetricTile } from "./components/MetricTile";
import { Badge } from "./components/Badge";
import { Collapsible } from "./components/Collapsible";
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
    const updated = {
      ...config,
      [section]: { ...(config as any)[section], [key]: value },
    };
    setConfig(updated);
    if (botRunning) {
      updateConfig(updated).catch(console.error);
    }
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
    <div className="min-h-screen bg-page text-slate-200 p-4 md:p-6">
      <div className="max-w-7xl mx-auto space-y-6">

        {/* ── Header ── */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-bold text-slate-100 tracking-tight">Sigma Kapital</h1>
            <span className="text-[10px] text-slate-500/70 tracking-wide">Dry-Run Simulation Engine v0.1.0</span>
          </div>
          <div className="flex items-center gap-3">
            <Badge status={wsStatus as any} label={wsStatus} />
            {botRunning ? (
              <button onClick={handleStop}
                className="relative px-5 py-2 rounded-xl text-xs font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/15 hover:bg-rose-500/20 transition-all group">
                <span className="absolute inset-0 rounded-xl animate-pulse bg-rose-500/5 group-hover:bg-transparent" />
                Botu Durdur
              </button>
            ) : (
              <button onClick={handleStart}
                disabled={activeSymbols.length === 0 || loading}
                className="px-5 py-2 rounded-xl text-xs font-semibold bg-sky-500/10 text-sky-400 border border-sky-500/15 hover:bg-sky-500/20 transition-all disabled:opacity-30 disabled:cursor-not-allowed">
                {loading ? "Tarama yapiliyor..." : "Botu Baslat"}
              </button>
            )}
          </div>
        </div>

        {/* ── Pair Selection ── */}
        <div className="bg-card rounded-2xl shadow-card p-5">
          <div className="flex items-center gap-3 mb-3">
            <h2 className="text-xs font-semibold text-slate-300 tracking-tight">Pair Secimi</h2>
            <span className="text-[10px] text-slate-500/60">{activeSymbols.length}/50</span>
          </div>
          <div className="flex gap-3 mb-3">
            <div className="relative flex-1 max-w-xs">
              <input type="text" value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Pair ara... (BTCUSDT)"
                className="w-full bg-input border border-input rounded-xl px-3.5 py-2 text-sm text-slate-200 placeholder-slate-600/60 focus:outline-none focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 transition-all" />
              {searchQuery && filteredSymbols.length > 0 && (
                <div className="absolute z-10 top-full left-0 right-0 mt-1 bg-card border border-card rounded-xl max-h-48 overflow-y-auto shadow-[0_8px_24px_rgba(0,0,0,0.4)]">
                  {filteredSymbols.slice(0, 20).map((s) => (
                    <button key={s} onClick={() => handleAddPair(s)}
                      className="w-full text-left px-3.5 py-2 text-xs text-slate-300 hover:bg-sky-500/10 transition-colors first:rounded-t-xl last:rounded-b-xl">
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
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-sky-500/8 text-[11px] text-slate-300 hover:bg-rose-500/15 hover:text-rose-400 transition-all border border-slate-700/20">
                  {s} <span className="text-slate-500 text-[10px]">x</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ── Config Panel (collapsible) ── */}
        {config && (
          <Collapsible
            title="Ayarlar"
            summary={`${formatNum(config.trading.initial_balance, 0)} USDT | ${config.trading.margin_per_trade} margin | ${config.trading.leverage}x`}
            defaultCollapsed={botRunning}
          >
            <div className="space-y-3">
              <div className="bg-card-inner rounded-xl p-4">
                <h2 className="text-[9px] font-semibold text-sky-400/50 uppercase tracking-[0.12em] mb-3">Trading</h2>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                  {[
                    { section: "trading", key: "initial_balance", label: "Ana Kasa (USDT)", step: "10" },
                    { section: "trading", key: "margin_per_trade", label: "Margin/Trade (USDT)", step: "10" },
                    { section: "trading", key: "leverage", label: "Kaldirac", step: "1" },
                  ].map(({ section, key, label, step }) => (
                    <label key={key} className="space-y-1">
                      <span className="text-slate-500/60 text-[9px] uppercase tracking-wide">{label}</span>
                      <input type="number" step={step}
                        value={(config as any)[section][key]}
                        onChange={(e) => handleConfigChange(section, key, +e.target.value)}
                        className="w-full bg-input border border-input rounded-xl px-3 py-2 text-sm font-mono text-slate-100 focus:outline-none focus:border-sky-500/30 focus:ring-1 focus:ring-sky-500/10 transition-all" />
                    </label>
                  ))}
                  <label className="space-y-1">
                    <span className="text-slate-500/60 text-[9px] uppercase tracking-wide">Islem Tipi</span>
                    <select value={config.trading.trade_type}
                      onChange={(e) => handleConfigChange("trading", "trade_type", e.target.value)}
                      className="w-full bg-input border border-input rounded-xl px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-sky-500/30 transition-all">
                      {["BOTH","LONG","SHORT"].map(t => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </label>
                </div>
              </div>

              {!botRunning && (
                <div className="bg-card-inner rounded-xl p-4">
                  <div className="flex items-center gap-2">
                    <h2 className="text-[9px] font-semibold text-sky-400/50 uppercase tracking-[0.12em]">
                      Adaptive PMax — {config.strategy.timeframes[0]?.label || "3m"}
                    </h2>
                    <span className="text-[9px] px-2 py-0.5 rounded-full bg-teal-500/8 text-teal-400/80 font-mono">
                      Dinamik
                    </span>
                  </div>
                  <p className="text-[10px] text-slate-500/60 mt-1.5">
                    Parametreler volatiliteye gore otomatik ayarlanir (settings.yaml)
                  </p>
                </div>
              )}
            </div>
          </Collapsible>
        )}

        {/* ── Summary Metrics ── */}
        {stats && (
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
            <MetricTile label="Bakiye" value={`${formatNum(stats.current_balance, 2)} USDT`} color="text-sky-300" />
            <MetricTile label="Kaldirac" value={`${stats.leverage}x`} />
            <MetricTile label="Aktif Pair" value={status?.active_symbols.length || 0} />
            <MetricTile label="Toplam Islem" value={stats.total_trades} />
            <MetricTile label="Win Rate" value={`${formatNum(stats.win_rate, 1)}%`}
              color={stats.win_rate >= 50 ? "text-teal-400" : "text-rose-400"} />
            <MetricTile label="Fiyat Kaynak" value={wsStatus}
              color={wsStatus === "WS LIVE" ? "text-teal-400" : wsStatus === "REST" ? "text-amber-400" : "text-slate-500"} />
          </div>
        )}

        {/* ── PnL Summary ── */}
        {totals && (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
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
          <div className="flex gap-5 text-[10px] text-slate-500/60 px-1 font-mono">
            <span>Maker: {formatNum(fees.maker, 4)}</span>
            <span>Taker: {formatNum(fees.taker, 4)}</span>
            <span>Total: {formatNum(fees.total, 4)} USDT</span>
          </div>
        )}

        {/* ── Pair Grid ── */}
        {status?.pair_summaries && Object.keys(status.pair_summaries).length > 0 && (
          <div>
            <h2 className="text-xs font-semibold text-slate-400/80 mb-4 tracking-tight">Pair Durumlari</h2>
            <PairGrid pairs={status.pair_summaries} />
          </div>
        )}

        {/* ── PMax Chart ── */}
        {botRunning && activeSymbols.length > 0 && (
          <PMaxChart key={activeSymbols[0]} symbol={activeSymbols[0]} botRunning={botRunning} title={`${activeSymbols[0]} - Trend Inventory`} />
        )}

        {/* ── Open Positions (collapsible) ── */}
        {status?.positions && status.positions.length > 0 && (
          <Collapsible
            title="Acik Pozisyonlar"
            summary={`${status.positions.length} pozisyon`}
          >
            <PositionTable positions={status.positions} />
          </Collapsible>
        )}

        {/* ── Trade History & Signal Log — side by side, collapsible ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Trade History */}
          <Collapsible
            title="Islem Gecmisi"
            summary={`${status?.trade_log?.length || 0} islem`}
          >
            <TradeTable trades={status?.trade_log || []} />
          </Collapsible>

          {/* Signal Log — default collapsed */}
          {status?.signal_log && status.signal_log.length > 0 && (
            <Collapsible
              title="Sinyal Gecmisi"
              summary={(() => {
                const last = status.signal_log[status.signal_log.length - 1];
                return last ? `Son: ${last.symbol} ${last.side} ${last.time}` : "";
              })()}
              defaultCollapsed={true}
            >
              <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-[#0b1a2e]">
                    <tr className="text-[9px] uppercase tracking-[0.1em] text-slate-500/60 border-b border-card">
                      <th className="text-left py-2.5 px-3">Zaman</th>
                      <th className="text-left py-2.5 px-3">Symbol</th>
                      <th className="text-center py-2.5">Side</th>
                      <th className="text-right py-2.5 px-3">Fiyat</th>
                      <th className="text-right py-2.5 px-3">RSI</th>
                      <th className="text-left py-2.5 px-3">Kaynak</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status.signal_log.map((s, i) => (
                      <tr key={i} className="border-b border-card hover:bg-black/5 dark:hover:bg-white/5 transition-colors">
                        <td className="py-2 px-3 text-muted font-mono">{s.time}</td>
                        <td className="py-2 px-3 font-semibold text-primary">{s.symbol}</td>
                        <td className="py-2 text-center">
                          <span className={`px-2.5 py-0.5 rounded-full text-[9px] font-semibold ${
                            s.side === "LONG" ? "bg-teal-500/10 text-teal-400" : "bg-rose-500/10 text-rose-400"
                          }`}>{s.side}</span>
                        </td>
                        <td className="py-2 px-3 text-right font-mono text-slate-200">{formatNum(s.price, 4)}</td>
                        <td className="py-2 px-3 text-right font-mono text-slate-500/70">{s.rsi}</td>
                        <td className="py-2 px-3 text-slate-500/70">{s.source}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Collapsible>
          )}
        </div>

        <div className="text-center text-[9px] text-slate-600/50 pb-6 tracking-wide">
          Sigma Kapital Trading Technologies & Market Making Services
        </div>
      </div>
    </div>
  );
}
