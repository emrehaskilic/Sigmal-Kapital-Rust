const BASE = "http://localhost:8000";
const OPTIMIZER_BASE = "http://localhost:8055";

export async function fetchSymbols(): Promise<{ symbols: string[]; count: number }> {
  const res = await fetch(`${BASE}/api/symbols`);
  return res.json();
}

export async function fetchConfig() {
  const res = await fetch(`${BASE}/api/config`);
  return res.json();
}

export async function updateConfig(body: object) {
  const res = await fetch(`${BASE}/api/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export async function startBot(symbols: string[]) {
  const res = await fetch(`${BASE}/api/bot/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbols }),
  });
  return res.json();
}

export async function stopBot() {
  const res = await fetch(`${BASE}/api/bot/stop`, { method: "POST" });
  return res.json();
}

export async function fetchStatus() {
  const res = await fetch(`${BASE}/api/status`);
  return res.json();
}

// ── Backtest API ──

export async function runBacktest(symbols: string[], lookbackDays: number, _config: object) {
  // Use fast numpy engine instead of slow candle-by-candle backtester
  const symbol = symbols[0] || "ETHUSDT";
  const res = await fetch(`${BASE}/api/backtest/fast`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, days: lookbackDays }),
  });
  return res.json();
}

export async function fetchBacktestStatus() {
  const res = await fetch(`${BASE}/api/backtest/fast/status`);
  const data = await res.json();
  // Map fast status to expected format
  return {
    running: data.running,
    progress: data.progress,
    status: data.running ? "simulating" : (data.error ? "error" : (data.progress >= 100 ? "done" : "idle")),
    error: data.error,
  };
}

export async function fetchBacktestResults() {
  const res = await fetch(`${BASE}/api/backtest/fast/results`);
  return res.json();
}

export async function resetBacktest() {
  const res = await fetch(`${BASE}/api/backtest/reset`, { method: "POST" });
  return res.json();
}

// ── Live Trading API ──

export async function liveSetKeys(apiKey: string, apiSecret: string, testnet: boolean) {
  const res = await fetch(`${BASE}/api/live/keys`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret, testnet }),
  });
  return res.json();
}

export async function liveGetBalance() {
  const res = await fetch(`${BASE}/api/live/balance`);
  return res.json();
}

export async function liveGetExchangePositions() {
  const res = await fetch(`${BASE}/api/live/positions`);
  return res.json();
}

export async function liveStart(
  pairConfigs: Record<string, { margin: number; leverage: number }>,
  protection?: { max_drawdown_pct: number; max_total_margin_pct: number; max_open_positions: number },
  strategy?: { use_alternate_signals: boolean; alternate_multiplier: number },
) {
  const res = await fetch(`${BASE}/api/live/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pair_configs: pairConfigs, protection, strategy }),
  });
  return res.json();
}

export async function liveStop() {
  const res = await fetch(`${BASE}/api/live/stop`, { method: "POST" });
  return res.json();
}

export async function liveEmergencyClose() {
  const res = await fetch(`${BASE}/api/live/emergency-close`, { method: "POST" });
  return res.json();
}

export async function liveStatus() {
  const res = await fetch(`${BASE}/api/live/status`);
  return res.json();
}

export async function liveUpdateProtection(settings: {
  max_drawdown_pct?: number;
  max_total_margin_pct?: number;
  max_open_positions?: number;
}) {
  const res = await fetch(`${BASE}/api/live/protection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  return res.json();
}

export async function liveGetProtection() {
  const res = await fetch(`${BASE}/api/live/protection`);
  return res.json();
}

export async function liveResetCircuitBreaker() {
  const res = await fetch(`${BASE}/api/live/reset-circuit-breaker`, { method: "POST" });
  return res.json();
}

// ── Pipeline V2 API (Optimizer Backend — port 8055) ──

export async function v2Init(symbol: string, timeframe: string = "3m", n_trials: number = 1000) {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/init`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, timeframe, n_trials }),
  });
  return res.json();
}

export async function v2GetState() {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/state`);
  return res.json();
}

export async function v2StepStart(step: string) {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/step/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ step }),
  });
  return res.json();
}

export async function v2StepSelect(step: string, selected_index: number) {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/step/select`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ step, selected_index }),
  });
  return res.json();
}

export async function v2UpdateSettings(settings: { timeframe?: string; n_trials?: number }) {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  return res.json();
}

export async function v2Stop() {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/stop`, { method: "POST" });
  return res.json();
}

export async function v2Reset() {
  const res = await fetch(`${OPTIMIZER_BASE}/api/v2/reset`, { method: "POST" });
  return res.json();
}
