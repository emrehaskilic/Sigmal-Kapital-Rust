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
  liveGetExchangePositions,
  liveUpdateProtection,
  liveResetCircuitBreaker,
} from "../api";
import { MetricTile } from "../components/MetricTile";
import { Badge } from "../components/Badge";
import { PositionTable } from "../components/PositionTable";
import { PairGrid } from "../components/PairGrid";
import { TradeTable } from "../components/TradeTable";
import { formatNum, pnlColor } from "../utils";

interface PairConfig {
  margin: number;
  leverage: number;
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

  // Pair selection & config
  const [allSymbols, setAllSymbols] = useState<string[]>([]);
  const [selectedPairs, setSelectedPairs] = useState<string[]>([]);
  const [pairConfigs, setPairConfigs] = useState<Record<string, PairConfig>>({});
  const [searchQuery, setSearchQuery] = useState("");

  // Strategy settings
  const [useAlternateSignals, setUseAlternateSignals] = useState(true);
  const [alternateMultiplier, setAlternateMultiplier] = useState(8);

  // Protection settings
  const [maxDrawdown, setMaxDrawdown] = useState(40);
  const [maxMarginPct, setMaxMarginPct] = useState(70);
  const [maxPositions, setMaxPositions] = useState(5);

  // Live state
  const [liveRunning, setLiveRunning] = useState(false);
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [exchangePositions, setExchangePositions] = useState<any[]>([]);
  const pollRef = useRef<number | null>(null);

  // Load symbols and config on mount
  useEffect(() => {
    fetchSymbols().then((d) => setAllSymbols(d.symbols));
    fetchConfig().then((cfg) => {
      if (cfg?.strategy) {
        setUseAlternateSignals(cfg.strategy.use_alternate_signals ?? true);
        setAlternateMultiplier(cfg.strategy.alternate_multiplier ?? 8);
      }
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
      [sym]: { margin: 50, leverage: 10 },
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

    const res = await liveStart(pairConfigs, {
      max_drawdown_pct: maxDrawdown,
      max_total_margin_pct: maxMarginPct,
      max_open_positions: maxPositions,
    }, {
      use_alternate_signals: useAlternateSignals,
      alternate_multiplier: alternateMultiplier,
    });
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

  const handleUpdateProtection = async () => {
    await liveUpdateProtection({
      max_drawdown_pct: maxDrawdown,
      max_total_margin_pct: maxMarginPct,
      max_open_positions: maxPositions,
    });
  };

  const handleResetCircuitBreaker = async () => {
    await liveResetCircuitBreaker();
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
    (sum, sym) => sum + (pairConfigs[sym]?.margin || 0),
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
        {!keysValid && !liveRunning && (
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
                Baglan
              </button>
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

        {/* ── Strategy Settings ── */}
        {keysValid && !liveRunning && (
          <div className="bg-[#0a1628] rounded-xl border border-blue-500/[0.08] p-4 space-y-3">
            <h2 className="text-sm font-semibold text-slate-300">Strateji Ayarlari</h2>
            <div className="flex items-center gap-4">
              <label className="space-y-0.5 flex flex-col">
                <span className="text-slate-500 text-[10px] uppercase">Alternate Signals</span>
                <div className="flex items-center gap-2 h-[30px]">
                  <input type="checkbox"
                    checked={useAlternateSignals}
                    onChange={(e) => setUseAlternateSignals(e.target.checked)}
                    className="w-4 h-4 accent-sky-500 bg-[#050a14] border-blue-500/[0.08] rounded" />
                  <span className="text-slate-400 text-[11px]">{useAlternateSignals ? "ON" : "OFF"}</span>
                  {useAlternateSignals && (
                    <input type="number" step="1" min="1"
                      value={alternateMultiplier}
                      onChange={(e) => setAlternateMultiplier(+e.target.value)}
                      className="w-12 bg-[#050a14] border border-blue-500/[0.08] rounded px-1.5 py-0.5 text-xs font-mono text-slate-200" />
                  )}
                </div>
              </label>
            </div>
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

            {/* Per-pair config table */}
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
                      const pc = pairConfigs[sym] || { margin: 50, leverage: 10 };
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

        {/* ── Account Protection Settings ── */}
        {keysValid && (
          <div className="bg-[#0a1628] rounded-xl border border-amber-500/20 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-amber-400">Hesap Koruma Ayarlari</h2>
              {liveRunning && (
                <button onClick={handleUpdateProtection}
                  className="px-3 py-1 rounded-lg text-[10px] font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/25 hover:bg-amber-500/25 transition-colors">
                  Guncelle
                </button>
              )}
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <label className="space-y-1">
                <span className="text-slate-400 text-[10px] uppercase">Max Drawdown (%)</span>
                <div className="flex items-center gap-2">
                  <input type="range" min="5" max="80" step="5"
                    value={maxDrawdown}
                    onChange={(e) => setMaxDrawdown(+e.target.value)}
                    className="flex-1 accent-amber-500 h-1.5" />
                  <input type="number" min="5" max="80" step="5"
                    value={maxDrawdown}
                    onChange={(e) => setMaxDrawdown(+e.target.value)}
                    className="w-16 bg-[#050a14] border border-blue-500/[0.08] rounded px-2 py-1 text-center text-xs font-mono text-amber-400" />
                </div>
                <span className="text-[9px] text-slate-500">Bakiye %{maxDrawdown} duserse yeni islem durdurulur</span>
              </label>
              <label className="space-y-1">
                <span className="text-slate-400 text-[10px] uppercase">Max Toplam Margin (%)</span>
                <div className="flex items-center gap-2">
                  <input type="range" min="10" max="100" step="5"
                    value={maxMarginPct}
                    onChange={(e) => setMaxMarginPct(+e.target.value)}
                    className="flex-1 accent-amber-500 h-1.5" />
                  <input type="number" min="10" max="100" step="5"
                    value={maxMarginPct}
                    onChange={(e) => setMaxMarginPct(+e.target.value)}
                    className="w-16 bg-[#050a14] border border-blue-500/[0.08] rounded px-2 py-1 text-center text-xs font-mono text-amber-400" />
                </div>
                <span className="text-[9px] text-slate-500">Tum pozisyonlarin toplam margin'i bakiyenin %{maxMarginPct}'ini asamaz</span>
              </label>
              <label className="space-y-1">
                <span className="text-slate-400 text-[10px] uppercase">Max Acik Pozisyon</span>
                <div className="flex items-center gap-2">
                  <input type="range" min="1" max="20" step="1"
                    value={maxPositions}
                    onChange={(e) => setMaxPositions(+e.target.value)}
                    className="flex-1 accent-amber-500 h-1.5" />
                  <input type="number" min="1" max="20" step="1"
                    value={maxPositions}
                    onChange={(e) => setMaxPositions(+e.target.value)}
                    className="w-16 bg-[#050a14] border border-blue-500/[0.08] rounded px-2 py-1 text-center text-xs font-mono text-amber-400" />
                </div>
                <span className="text-[9px] text-slate-500">Ayni anda en fazla {maxPositions} pozisyon acik olabilir</span>
              </label>
            </div>

            {/* Circuit Breaker Alert */}
            {liveRunning && stats?.circuit_breaker && (
              <div className="mt-2 bg-red-500/10 border border-red-500/30 rounded-lg p-3 flex items-center justify-between">
                <div>
                  <span className="text-xs font-semibold text-red-400">CIRCUIT BREAKER AKTIF</span>
                  <p className="text-[10px] text-red-300 mt-0.5">{stats.circuit_breaker_reason}</p>
                </div>
                <button onClick={handleResetCircuitBreaker}
                  className="px-3 py-1.5 rounded-lg text-[10px] font-semibold bg-red-500/15 text-red-400 border border-red-500/25 hover:bg-red-500/25 transition-colors">
                  Sifirla
                </button>
              </div>
            )}

            {/* Live Protection Stats */}
            {liveRunning && stats && (
              <div className="grid grid-cols-3 gap-2 mt-2">
                <div className="bg-[#050a14]/60 rounded-lg px-3 py-2 text-center">
                  <div className="text-[9px] text-slate-500 uppercase">Drawdown</div>
                  <div className={`text-sm font-mono font-semibold ${
                    stats.drawdown_pct > maxDrawdown * 0.7 ? "text-red-400" : "text-slate-200"
                  }`}>
                    %{formatNum(stats.drawdown_pct, 1)} <span className="text-[9px] text-slate-500">/ %{maxDrawdown}</span>
                  </div>
                </div>
                <div className="bg-[#050a14]/60 rounded-lg px-3 py-2 text-center">
                  <div className="text-[9px] text-slate-500 uppercase">Acik Pozisyon</div>
                  <div className={`text-sm font-mono font-semibold ${
                    stats.open_positions >= maxPositions ? "text-red-400" : "text-slate-200"
                  }`}>
                    {stats.open_positions} <span className="text-[9px] text-slate-500">/ {maxPositions}</span>
                  </div>
                </div>
                <div className="bg-[#050a14]/60 rounded-lg px-3 py-2 text-center">
                  <div className="text-[9px] text-slate-500 uppercase">Sync Uyarilari</div>
                  <div className={`text-sm font-mono font-semibold ${
                    (stats.sync_warnings?.length || 0) > 0 ? "text-yellow-400" : "text-slate-200"
                  }`}>
                    {stats.sync_warnings?.length || 0}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

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
                      Ilk sinyal bekleniyor — reversal geldiginde islem baslar
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
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

        <div className="text-center text-[10px] text-slate-600 pb-4">
          Sigma Kapital Trading Technologies & Market Making Services
        </div>
      </div>
    </div>
  );
}
