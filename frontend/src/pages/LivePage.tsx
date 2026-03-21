import { useState, useEffect, useRef } from "react";
import {
  fetchSymbols,
  fetchConfig,
  liveSetKeys,
  liveGetBalance,
  liveStart,
  liveStop,
  liveStatus,
  liveEmergencyClose,
  liveOrderHistory,
  liveGetExchangePositions,
  monitorCreateToken,
  monitorListTokens,
  monitorDeleteToken,
} from "../api";
import { MetricTile } from "../components/MetricTile";
import { Badge } from "../components/Badge";
import { PositionTable } from "../components/PositionTable";
import { PairGrid } from "../components/PairGrid";
import { TradeTable } from "../components/TradeTable";
import { PMaxChart } from "../components/PMaxChart";
import { formatNum, pnlColor } from "../utils";

interface PairConfig {
  margin: number;
  leverage: number;
}

/* ── Multi-Pair Chart with Tab/Grid Layout ── */
function MultiPairChart({ symbols, botRunning }: { symbols: string[]; botRunning: boolean }) {
  const [activeTab, setActiveTab] = useState<string>(symbols[0] || "");
  const [viewMode, setViewMode] = useState<"tabs" | "grid">("tabs");

  // Sync active tab when symbols change
  useEffect(() => {
    if (symbols.length > 0 && !symbols.includes(activeTab)) {
      setActiveTab(symbols[0]);
    }
  }, [symbols, activeTab]);

  if (symbols.length === 0) return null;

  return (
    <div className="space-y-2">
      {/* Controls bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          {/* View mode toggle */}
          <div className="flex rounded-lg overflow-hidden border border-blue-500/[0.08]">
            <button
              onClick={() => setViewMode("tabs")}
              className={`px-3 py-1 text-[10px] font-semibold transition-colors ${
                viewMode === "tabs"
                  ? "bg-sky-500/15 text-sky-400"
                  : "bg-[#0a1628] text-slate-500 hover:text-slate-300"
              }`}
            >
              Tab
            </button>
            <button
              onClick={() => setViewMode("grid")}
              className={`px-3 py-1 text-[10px] font-semibold transition-colors ${
                viewMode === "grid"
                  ? "bg-sky-500/15 text-sky-400"
                  : "bg-[#0a1628] text-slate-500 hover:text-slate-300"
              }`}
            >
              Grid
            </button>
          </div>

          {/* Pair tabs — only in tab mode */}
          {viewMode === "tabs" && symbols.map((sym) => (
            <button
              key={sym}
              onClick={() => setActiveTab(sym)}
              className={`px-3 py-1.5 rounded-lg text-[11px] font-semibold transition-all ${
                sym === activeTab
                  ? "bg-teal-500/15 text-teal-400 ring-1 ring-teal-500/20"
                  : "bg-[#0a1628] text-slate-500 hover:text-slate-300 hover:bg-[#0d1e38]"
              }`}
            >
              {sym.replace("USDT", "")}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-slate-500">{symbols.length} pair aktif</span>
      </div>

      {/* Tab mode — single chart */}
      {viewMode === "tabs" && (
        <PMaxChart
          key={activeTab}
          symbol={activeTab}
          botRunning={botRunning}
          title={`${activeTab} — Trend Inventory`}
          mode="live"
        />
      )}

      {/* Grid mode — all charts */}
      {viewMode === "grid" && (
        <div className={`grid gap-3 ${
          symbols.length === 1 ? "grid-cols-1" :
          symbols.length <= 4 ? "grid-cols-1 lg:grid-cols-2" :
          "grid-cols-1 lg:grid-cols-2 xl:grid-cols-3"
        }`}>
          {symbols.map((sym) => (
            <PMaxChart
              key={sym}
              symbol={sym}
              botRunning={botRunning}
              title={`${sym} — Trend Inventory`}
              mode="live"
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function LivePage() {
  // API Keys
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [testnet, setTestnet] = useState(false);
  const [keysValid, setKeysValid] = useState(false);
  const [keysError, setKeysError] = useState("");

  // Balance
  const [balance, setBalance] = useState(0);
  const [available, setAvailable] = useState(0);

  // Config from settings.yaml
  const [defaultMargin, setDefaultMargin] = useState(300);
  const [defaultLeverage, setDefaultLeverage] = useState(25);

  // Pair selection & config
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [selectedPairs, setSelectedPairs] = useState<string[]>([]);
  const [pairConfigs, setPairConfigs] = useState<Record<string, PairConfig>>({});
  const [searchQuery, setSearchQuery] = useState("");

  // Protection values from settings.yaml (read-only)

  // Live state
  const [liveRunning, setLiveRunning] = useState(false);
  const [status, setStatus] = useState<any>(null);
  const [orderHistory, setOrderHistory] = useState<any>(null);
  const [showOrderHistory, setShowOrderHistory] = useState(false);
  const [loading, setLoading] = useState(false);
  const [exchangePositions, setExchangePositions] = useState<any[]>([]);
  const pollRef = useRef<number | null>(null);

  // Monitor tokens
  const [monitorTokens, setMonitorTokens] = useState<{ token: string; created_at: number }[]>([]);
  const [monitorCopied, setMonitorCopied] = useState("");

  // Load symbols, config, and monitor tokens on mount
  useEffect(() => {
    fetchSymbols().then((d) => setAllSymbols(d.symbols));
    fetchConfig().then((cfg) => {
      if (cfg?.trading) {
        setDefaultMargin(cfg.trading.margin_per_trade ?? 300);
        setDefaultLeverage(cfg.trading.leverage ?? 25);
      }
    });
    monitorListTokens().then((d) => {
      if (d.tokens) setMonitorTokens(d.tokens);
    });
  }, []);

  // Poll live status
  useEffect(() => {
    if (liveRunning) {
      const poll = () => {
        liveStatus().then(setStatus).catch(console.error);
      };
      poll();
      pollRef.current = window.setInterval(poll, 1000);
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [liveRunning]);

  // --- Handlers ---

  const handleSetKeys = async () => {
    setKeysError("");
    const res = await liveSetKeys(apiKey, apiSecret, testnet);
    if (res.error) {
      setKeysError(res.error);
      setKeysValid(false);
    } else {
      setKeysValid(true);
      setBalance(res.balance);
      setAvailable(res.available);
    }
  };

  const handleRefreshBalance = async () => {
    const res = await liveGetBalance();
    if (!res.error) {
      setBalance(res.balance);
      setAvailable(res.available);
    }
  };

  const handleAddPair = (sym: string) => {
    if (selectedPairs.includes(sym) || selectedPairs.length >= 20) return;
    setSelectedPairs((prev) => [...prev, sym]);
    setPairConfigs((prev) => ({
      ...prev,
      [sym]: { margin: defaultMargin, leverage: defaultLeverage },
    }));
    setSearchQuery("");
  };

  const handleRemovePair = (sym: string) => {
    setSelectedPairs((prev) => prev.filter((s) => s !== sym));
    setPairConfigs((prev) => {
      const next = { ...prev };
      delete next[sym];
      return next;
    });
  };

  const handlePairConfigChange = (sym: string, key: keyof PairConfig, value: number) => {
    setPairConfigs((prev) => ({
      ...prev,
      [sym]: { ...prev[sym], [key]: value },
    }));
  };

  const handleStart = async () => {
    if (selectedPairs.length === 0 || !keysValid) return;
    setLoading(true);

    // Fetch exchange positions before starting
    const posRes = await liveGetExchangePositions();
    if (posRes.positions) {
      setExchangePositions(posRes.positions);
    }

    const finalConfigs: Record<string, PairConfig> = {};
    for (const sym of selectedPairs) {
      const pc = pairConfigs[sym] || { margin: defaultMargin, leverage: defaultLeverage };
      finalConfigs[sym] = { margin: pc.margin, leverage: pc.leverage };
    }

    const res = await liveStart(finalConfigs);
    if (res.error) {
      setKeysError(res.error);
      setLoading(false);
      return;
    }
    setLiveRunning(true);
    setLoading(false);
  };

  const handleStop = async () => {
    await liveStop();
    setLiveRunning(false);
    setStatus(null);
  };

  const handleEmergencyClose = async () => {
    if (!confirm("TUM POZISYONLARI KAPATMAK ISTEDIGINIZE EMIN MISINIZ?")) return;
    const res = await liveEmergencyClose();
    alert(`${res.trades_closed || 0} pozisyon kapatildi.`);
  };

  const handleCreateMonitorLink = async () => {
    const res = await monitorCreateToken();
    if (res.token) {
      setMonitorTokens((prev) => [...prev, { token: res.token, created_at: Date.now() / 1000 }]);
    }
  };

  const handleDeleteMonitorToken = async (token: string) => {
    await monitorDeleteToken(token);
    setMonitorTokens((prev) => prev.filter((t) => t.token !== token));
  };

  const handleCopyMonitorLink = (token: string) => {
    const url = `${window.location.origin}/sigmakapital/${token}`;
    navigator.clipboard.writeText(url);
    setMonitorCopied(token);
    setTimeout(() => setMonitorCopied(""), 2000);
  };

  // Filtered symbols for search
  const filteredSymbols = allSymbols
    .filter((s) => !selectedPairs.includes(s))
    .filter((s) => s.toLowerCase().includes(searchQuery.toLowerCase()));

  // Stats from status
  const stats = status?.stats;
  const totals = status?.totals;
  const liveBalance = status?.balance ?? balance;
  const liveAvailable = status?.available ?? available;

  // Total margin allocation
  const totalMarginAllocated = selectedPairs.reduce(
    (sum, sym) => sum + (pairConfigs[sym]?.margin || defaultMargin),
    0
  );

  return (
    <div className="min-h-screen bg-[#050a14] text-slate-200 p-4 md:p-6">
      <div className="max-w-7xl mx-auto space-y-5">

        {/* ── Header ── */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-slate-100">Sigma Kapital</h1>
            <span className="text-[11px] text-emerald-400 font-semibold">LIVE Trading Engine v0.1.0</span>
          </div>
          <div className="flex items-center gap-3">
            {liveRunning && (
              <Badge status="WS LIVE" label="LIVE" />
            )}
            {liveRunning ? (
              <div className="flex gap-2">
                <button onClick={handleEmergencyClose}
                  className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-orange-500/15 text-orange-400 border border-orange-500/25 hover:bg-orange-500/25 transition-colors">
                  Acil Kapat
                </button>
                <button onClick={handleStop}
                  className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-red-500/15 text-red-400 border border-red-500/25 hover:bg-red-500/25 transition-colors">
                  Botu Durdur
                </button>
              </div>
            ) : (
              <button onClick={handleStart}
                disabled={selectedPairs.length === 0 || !keysValid || loading}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/25 hover:bg-emerald-500/25 transition-colors disabled:opacity-30 disabled:cursor-not-allowed">
                {loading ? "Baslaniyor..." : "LIVE Baslat"}
              </button>
            )}
          </div>
        </div>

        {/* ── API Keys Section ── */}
        {!liveRunning && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4 space-y-3">
            <h2 className="text-sm font-semibold text-slate-300">Binance API Anahtarlari</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <label className="space-y-0.5">
                <span className="text-slate-500 text-[10px] uppercase">API Key</span>
                <input type="password" value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="Binance Futures API Key"
                  className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-emerald-500/50" />
              </label>
              <label className="space-y-0.5">
                <span className="text-slate-500 text-[10px] uppercase">API Secret</span>
                <input type="password" value={apiSecret}
                  onChange={(e) => setApiSecret(e.target.value)}
                  placeholder="Binance Futures API Secret"
                  className="w-full bg-[#050a14] border border-blue-500/[0.08] rounded-lg px-3 py-1.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-emerald-500/50" />
              </label>
            </div>
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-2 text-xs text-slate-400">
                <input type="checkbox" checked={testnet}
                  onChange={(e) => setTestnet(e.target.checked)}
                  className="w-4 h-4 accent-emerald-500" />
                Testnet Kullan
              </label>
              <button onClick={handleSetKeys}
                disabled={!apiKey || !apiSecret}
                className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/25 hover:bg-emerald-500/25 transition-colors disabled:opacity-30">
                {keysValid ? "Yeniden Baglan" : "Baglan"}
              </button>
              {keysValid && (
                <span className="flex items-center gap-1.5 text-[10px] text-emerald-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  Bagli
                </span>
              )}
            </div>
            {keysError && (
              <div className="text-xs text-red-400 bg-red-500/10 rounded-lg px-3 py-2">{keysError}</div>
            )}
          </div>
        )}

        {/* ── Balance Display ── */}
        {keysValid && (
          <div className="bg-[#0a1628] rounded-xl border border-emerald-500/20 p-4">
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-sm font-semibold text-emerald-400">Binance Futures Cuzdani</h2>
              {!liveRunning && (
                <button onClick={handleRefreshBalance}
                  className="text-[10px] text-slate-500 hover:text-slate-300 transition-colors">
                  Yenile
                </button>
              )}
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <MetricTile label="Toplam Bakiye" value={`${formatNum(liveBalance, 2)} USDT`} color="text-emerald-400" />
              <MetricTile label="Kullanilabilir" value={`${formatNum(liveAvailable, 2)} USDT`} color="text-sky-400" />
              <MetricTile label="Tahsis Edilen Margin" value={`${formatNum(totalMarginAllocated, 2)} USDT`}
                color={totalMarginAllocated > liveAvailable ? "text-red-400" : "text-slate-300"} />
              <MetricTile label="Aktif Pair" value={selectedPairs.length} />
            </div>
            {totalMarginAllocated > liveAvailable && (
              <div className="mt-2 text-xs text-red-400 bg-red-500/10 rounded-lg px-3 py-1.5">
                Toplam margin tahsisi kullanilabilir bakiyeyi asiyor!
              </div>
            )}
          </div>
        )}

        {/* ── Pair Selection & Config ── */}
        {keysValid && !liveRunning && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4 space-y-4">
            <div className="flex items-center gap-3">
              <h2 className="text-sm font-semibold text-slate-300">Pair Secimi & Ayarlari</h2>
              <span className="text-[10px] text-slate-500">{selectedPairs.length}/20</span>
            </div>

            {/* Search */}
            <div className="relative max-w-xs">
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

            {/* Per-pair config table — sadece margin, leverage sabit */}
            {selectedPairs.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                      <th className="text-left py-2 px-2">Pair</th>
                      <th className="text-center py-2 px-2">Margin (USDT)</th>
                      <th className="text-center py-2 px-2">Kaldirac</th>
                      <th className="text-center py-2 px-2">Notional</th>
                      <th className="text-center py-2 px-2"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedPairs.map((sym) => {
                      const pc = pairConfigs[sym] || { margin: defaultMargin, leverage: defaultLeverage };
                      return (
                        <tr key={sym} className="border-b border-blue-500/[0.06]">
                          <td className="py-2 px-2 font-semibold text-slate-200">{sym}</td>
                          <td className="py-2 px-2 text-center">
                            <input type="number" step="10" min="5"
                              value={pc.margin}
                              onChange={(e) => handlePairConfigChange(sym, "margin", +e.target.value)}
                              className="w-20 bg-[#050a14] border border-blue-500/[0.08] rounded px-2 py-1 text-center text-xs font-mono text-slate-200" />
                          </td>
                          <td className="py-2 px-2 text-center">
                            <input type="number" step="1" min="1" max="125"
                              value={pc.leverage}
                              onChange={(e) => handlePairConfigChange(sym, "leverage", +e.target.value)}
                              className="w-16 bg-[#050a14] border border-blue-500/[0.08] rounded px-2 py-1 text-center text-xs font-mono text-slate-200" />
                          </td>
                          <td className="py-2 px-2 text-center font-mono text-slate-400">
                            {formatNum(pc.margin * pc.leverage, 0)} USDT
                          </td>
                          <td className="py-2 px-2 text-center">
                            <button onClick={() => handleRemovePair(sym)}
                              className="text-slate-500 hover:text-red-400 transition-colors text-sm">
                              x
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* Account protection values come from settings.yaml */}

        {/* ── Exchange Positions (shown on start, before signals) ── */}
        {liveRunning && exchangePositions.length > 0 && !status?.positions?.length && (
          <div className="bg-[#0a1628] rounded-xl border border-yellow-500/20 p-4">
            <h2 className="text-sm font-semibold text-yellow-400 mb-3">Binance Acik Pozisyonlar (Onceden Mevcut)</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                    <th className="text-left py-2 px-2">Symbol</th>
                    <th className="text-center py-2">Side</th>
                    <th className="text-right py-2 px-2">Amount</th>
                    <th className="text-right py-2 px-2">Entry Price</th>
                    <th className="text-right py-2 px-2">Unrealized PnL</th>
                    <th className="text-center py-2 px-2">Leverage</th>
                  </tr>
                </thead>
                <tbody>
                  {exchangePositions.map((p, i) => (
                    <tr key={i} className="border-b border-blue-500/[0.06]">
                      <td className="py-1.5 px-2 font-semibold">{p.symbol}</td>
                      <td className="py-1.5 text-center">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                          p.side === "LONG" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"
                        }`}>{p.side}</span>
                      </td>
                      <td className="py-1.5 px-2 text-right font-mono">{p.amount}</td>
                      <td className="py-1.5 px-2 text-right font-mono">{formatNum(p.entry_price, 4)}</td>
                      <td className={`py-1.5 px-2 text-right font-mono ${pnlColor(p.unrealized_pnl)}`}>
                        {formatNum(p.unrealized_pnl, 4, true)} USDT
                      </td>
                      <td className="py-1.5 px-2 text-center">{p.leverage}x</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* ── Live Summary Metrics ── */}
        {liveRunning && stats && (
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
            <MetricTile label="Bakiye" value={`${formatNum(liveBalance, 2)} USDT`} color="text-emerald-400" />
            <MetricTile label="Kullanilabilir" value={`${formatNum(liveAvailable, 2)} USDT`} color="text-sky-400" />
            <MetricTile label="Aktif Pair" value={status?.active_symbols?.length || 0} />
            <MetricTile label="Toplam Islem" value={stats.total_trades} />
            <MetricTile label="Win Rate" value={`${formatNum(stats.win_rate, 1)}%`}
              color={stats.win_rate >= 50 ? "text-emerald-400" : "text-red-400"} />
            <MetricTile label="Mod" value="LIVE" color="text-emerald-400" />
          </div>
        )}

        {/* ── PnL Summary ── */}
        {liveRunning && totals && (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <MetricTile label="Unrealized PnL" value={`${formatNum(totals.unrealized_pnl, 4, true)} USDT`} color={pnlColor(totals.unrealized_pnl)} />
            <MetricTile label="Realized PnL" value={`${formatNum(totals.realized_pnl, 4, true)} USDT`} color={pnlColor(totals.realized_pnl)} />
            <MetricTile label="Total PnL" value={`${formatNum(totals.total_pnl, 4, true)} USDT`} color={pnlColor(totals.total_pnl)} />
            <MetricTile label="Net PnL (- Fees)" value={`${formatNum(totals.net_pnl, 4, true)} USDT`} color={pnlColor(totals.net_pnl)} />
            <MetricTile label="Total Fees" value={`${formatNum(totals.total_fees, 4)} USDT`} color="text-slate-400" />
          </div>
        )}

        {/* ── Pair Grid with state indicators ── */}
        {liveRunning && status?.pair_summaries && Object.keys(status.pair_summaries).length > 0 && (
          <div>
            <h2 className="text-sm font-semibold text-slate-300 mb-3">Pair Durumlari</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {Object.entries(status.pair_summaries).map(([sym, pair]: [string, any]) => (
                <div key={sym} className={`rounded-xl border p-3 ${
                  pair.pair_state === "OBSERVING"
                    ? "bg-[#0a1628] border-yellow-500/20"
                    : pair.side === "LONG"
                      ? "bg-[#0a1628] border-emerald-500/20"
                      : pair.side === "SHORT"
                        ? "bg-[#0a1628] border-red-500/20"
                        : "bg-[#0a1628] border-blue-500/[0.08]"
                }`}>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-sm font-semibold text-slate-200">{sym}</span>
                    <div className="flex items-center gap-2">
                      {pair.pair_state === "OBSERVING" && (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-yellow-400/15 text-yellow-400">
                          GOZLEM
                        </span>
                      )}
                      {pair.pair_state === "ACTIVE" && (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-emerald-400/15 text-emerald-400">
                          AKTIF
                        </span>
                      )}
                      {pair.side && (
                        <span className={`px-1.5 py-0.5 rounded text-[9px] font-semibold ${
                          pair.side === "LONG" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"
                        }`}>{pair.side}</span>
                      )}
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-1 text-[10px] text-slate-400">
                    <div>Fiyat: <span className="text-slate-200 font-mono">{formatNum(pair.last_price, 4)}</span></div>
                    <div>RSI: <span className="text-slate-200 font-mono">{pair.rsi}</span></div>
                    <div>PnL: <span className={`font-mono ${pnlColor(pair.total_pnl)}`}>{formatNum(pair.total_pnl, 4, true)}</span></div>
                  </div>
                  {pair.pair_state === "OBSERVING" && (
                    <div className="mt-2 text-[9px] text-yellow-400/70">
                      Sinyal bekleniyor — trend degisiminde pozisyon acilacak
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Trend Inventory Charts — Multi-Pair Tabs ── */}
        {liveRunning && status?.active_symbols?.length > 0 && (
          <MultiPairChart symbols={status.active_symbols} botRunning={liveRunning} />
        )}

        {/* ── Open Positions ── */}
        {liveRunning && status?.positions && status.positions.length > 0 && (
          <div className="bg-[#0a1628] rounded-xl border border-emerald-500/20 p-4">
            <h2 className="text-sm font-semibold text-emerald-400 mb-3">Acik Pozisyonlar (LIVE)</h2>
            <PositionTable positions={status.positions} />
          </div>
        )}

        {/* ── Trade History ── */}
        {liveRunning && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
            <h2 className="text-sm font-semibold text-slate-300 mb-3">Islem Gecmisi</h2>
            <TradeTable trades={status?.trade_log || []} />
          </div>
        )}

        {/* ── Signal Log ── */}
        {liveRunning && status?.signal_log && status.signal_log.length > 0 && (
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
                  {status.signal_log.map((s: any, i: number) => (
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

        {/* ── Order History ── */}
        {liveRunning && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-slate-300">Emir Gecmisi</h2>
              <button
                onClick={async () => {
                  if (!showOrderHistory) {
                    try {
                      const data = await liveOrderHistory();
                      setOrderHistory(data);
                    } catch {}
                  }
                  setShowOrderHistory(!showOrderHistory);
                }}
                className="px-3 py-1 rounded-lg text-[11px] font-semibold bg-sky-500/15 text-sky-400 border border-sky-500/25 hover:bg-sky-500/25 transition-colors">
                {showOrderHistory ? "Gizle" : "Goster"}
              </button>
            </div>
            {showOrderHistory && orderHistory && (
              <div className="space-y-4">
                {/* Market Entries */}
                {orderHistory.market_entries?.length > 0 && (
                  <div>
                    <h3 className="text-[11px] font-semibold text-emerald-400 mb-2 uppercase tracking-wider">Market Emirleri (Entry)</h3>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                            <th className="text-left py-1.5 px-2">ID</th>
                            <th className="text-center py-1.5">Side</th>
                            <th className="text-right py-1.5 px-2">Qty</th>
                            <th className="text-right py-1.5 px-2">Avg Price</th>
                            <th className="text-center py-1.5">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderHistory.market_entries.map((o: any) => (
                            <tr key={o.orderId} className="border-b border-blue-500/[0.06]">
                              <td className="py-1 px-2 text-slate-500 font-mono">{o.orderId}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.side === "BUY" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"}`}>{o.side}</span></td>
                              <td className="py-1 px-2 text-right font-mono">{o.executedQty}</td>
                              <td className="py-1 px-2 text-right font-mono">{formatNum(o.avgPrice, 4)}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.status === "FILLED" ? "bg-emerald-400/15 text-emerald-400" : "bg-yellow-400/15 text-yellow-400"}`}>{o.status}</span></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* DCA Orders */}
                {orderHistory.dca_orders?.length > 0 && (
                  <div>
                    <h3 className="text-[11px] font-semibold text-sky-400 mb-2 uppercase tracking-wider">DCA Emirleri</h3>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                            <th className="text-left py-1.5 px-2">ID</th>
                            <th className="text-center py-1.5">Side</th>
                            <th className="text-right py-1.5 px-2">Qty</th>
                            <th className="text-right py-1.5 px-2">Fiyat</th>
                            <th className="text-right py-1.5 px-2">Avg Fill</th>
                            <th className="text-center py-1.5">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderHistory.dca_orders.map((o: any) => (
                            <tr key={o.orderId} className="border-b border-blue-500/[0.06]">
                              <td className="py-1 px-2 text-slate-500 font-mono">{o.orderId}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.side === "BUY" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"}`}>{o.side}</span></td>
                              <td className="py-1 px-2 text-right font-mono">{o.origQty}</td>
                              <td className="py-1 px-2 text-right font-mono">{formatNum(o.price, 4)}</td>
                              <td className="py-1 px-2 text-right font-mono">{o.avgPrice > 0 ? formatNum(o.avgPrice, 4) : "-"}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.status === "FILLED" ? "bg-emerald-400/15 text-emerald-400" : o.status === "NEW" ? "bg-yellow-400/15 text-yellow-400" : "bg-slate-400/15 text-slate-400"}`}>{o.status}</span></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* TP Orders */}
                {orderHistory.tp_orders?.length > 0 && (
                  <div>
                    <h3 className="text-[11px] font-semibold text-orange-400 mb-2 uppercase tracking-wider">TP Emirleri</h3>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                            <th className="text-left py-1.5 px-2">ID</th>
                            <th className="text-center py-1.5">Side</th>
                            <th className="text-right py-1.5 px-2">Qty</th>
                            <th className="text-right py-1.5 px-2">Fiyat</th>
                            <th className="text-right py-1.5 px-2">Avg Fill</th>
                            <th className="text-center py-1.5">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderHistory.tp_orders.map((o: any) => (
                            <tr key={o.orderId} className="border-b border-blue-500/[0.06]">
                              <td className="py-1 px-2 text-slate-500 font-mono">{o.orderId}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.side === "SELL" ? "bg-red-400/15 text-red-400" : "bg-emerald-400/15 text-emerald-400"}`}>{o.side}</span></td>
                              <td className="py-1 px-2 text-right font-mono">{o.origQty}</td>
                              <td className="py-1 px-2 text-right font-mono">{formatNum(o.price, 4)}</td>
                              <td className="py-1 px-2 text-right font-mono">{o.avgPrice > 0 ? formatNum(o.avgPrice, 4) : "-"}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${o.status === "FILLED" ? "bg-emerald-400/15 text-emerald-400" : o.status === "NEW" ? "bg-yellow-400/15 text-yellow-400" : "bg-slate-400/15 text-slate-400"}`}>{o.status}</span></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* Completed Trades (PnL) */}
                {orderHistory.trades?.length > 0 && (
                  <div>
                    <h3 className="text-[11px] font-semibold text-purple-400 mb-2 uppercase tracking-wider">Tamamlanan Islemler</h3>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-[10px] uppercase tracking-wider text-slate-500 border-b border-blue-500/[0.08]">
                            <th className="text-left py-1.5 px-2">Symbol</th>
                            <th className="text-center py-1.5">Side</th>
                            <th className="text-right py-1.5 px-2">Entry</th>
                            <th className="text-right py-1.5 px-2">Exit</th>
                            <th className="text-center py-1.5">Reason</th>
                            <th className="text-right py-1.5 px-2">PnL</th>
                            <th className="text-right py-1.5 px-2">Fee</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orderHistory.trades.map((t: any, i: number) => (
                            <tr key={i} className="border-b border-blue-500/[0.06]">
                              <td className="py-1 px-2 font-semibold">{t.symbol}</td>
                              <td className="py-1 text-center"><span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${t.side === "LONG" ? "bg-emerald-400/15 text-emerald-400" : "bg-red-400/15 text-red-400"}`}>{t.side}</span></td>
                              <td className="py-1 px-2 text-right font-mono">{formatNum(t.entry_price, 4)}</td>
                              <td className="py-1 px-2 text-right font-mono">{formatNum(t.exit_price, 4)}</td>
                              <td className="py-1 text-center text-slate-400">{t.exit_reason}</td>
                              <td className={`py-1 px-2 text-right font-mono font-semibold ${t.pnl_usdt >= 0 ? "text-emerald-400" : "text-red-400"}`}>${formatNum(t.pnl_usdt, 4)}</td>
                              <td className="py-1 px-2 text-right font-mono text-slate-500">${formatNum(t.fee_usdt, 4)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* ── Pair Configs (running mode) ── */}
        {liveRunning && status?.pair_configs && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4">
            <h2 className="text-sm font-semibold text-slate-300 mb-3">Pair Ayarlari</h2>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2 text-xs">
              {Object.entries(status.pair_configs).map(([sym, pc]: [string, any]) => (
                <div key={sym} className="bg-[#050a14]/60 rounded-lg px-3 py-2 flex items-center justify-between">
                  <span className="font-semibold text-slate-200">{sym}</span>
                  <span className="text-slate-400 font-mono">{pc.margin} USDT / {pc.leverage}x</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Monitor Link Yönetimi ── */}
        {liveRunning && (
          <div className="bg-[#0a1628] rounded-xl border border-purple-500/20 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-purple-400">Monitor Linkleri</h2>
              <button onClick={handleCreateMonitorLink}
                className="px-3 py-1.5 rounded-lg text-[11px] font-semibold bg-purple-500/15 text-purple-400 border border-purple-500/25 hover:bg-purple-500/25 transition-colors">
                Monitor Linki Olustur
              </button>
            </div>
            {monitorTokens.length === 0 && (
              <p className="text-[11px] text-slate-500">Henuz monitor linki olusturulmadi. Disaridan izlemek icin bir link olusturun.</p>
            )}
            {monitorTokens.map((mt) => {
              const url = `${window.location.origin}/sigmakapital/${mt.token}`;
              return (
                <div key={mt.token} className="flex items-center gap-2 bg-[#050a14]/60 rounded-lg px-3 py-2">
                  <span className="flex-1 text-[11px] font-mono text-slate-400 truncate">{url}</span>
                  <button onClick={() => handleCopyMonitorLink(mt.token)}
                    className={`px-2.5 py-1 rounded text-[10px] font-semibold transition-colors ${
                      monitorCopied === mt.token
                        ? "bg-emerald-500/15 text-emerald-400"
                        : "bg-sky-500/15 text-sky-400 hover:bg-sky-500/25"
                    }`}>
                    {monitorCopied === mt.token ? "Kopyalandi!" : "Kopyala"}
                  </button>
                  <button onClick={() => handleDeleteMonitorToken(mt.token)}
                    className="px-2.5 py-1 rounded text-[10px] font-semibold bg-red-500/15 text-red-400 hover:bg-red-500/25 transition-colors">
                    Sil
                  </button>
                </div>
              );
            })}
          </div>
        )}

        <div className="text-center text-[10px] text-slate-600 pb-4">
          Sigma Kapital Trading Technologies & Market Making Services
        </div>
      </div>
    </div>
  );
}
