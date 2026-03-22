interface WsHealth {
  uds_connected: boolean;
  ws_last_event_ms: number;
  ws_status: "LIVE" | "STALE" | "DISCONNECTED";
  heartbeat_age_ms: number;
  pending_orders: {
    order_id: number;
    symbol: string;
    type: string;
    side: string;
    price: number;
    age_s: number;
    last_verify_s: number;
  }[];
  total_tracked_orders: number;
  mismatches: number;
  watchdog_timeout_s: number;
}

const STATUS_STYLES: Record<string, { bg: string; dot: string; text: string; label: string }> = {
  LIVE: {
    bg: "bg-emerald-500/10 border-emerald-500/20",
    dot: "bg-emerald-400 animate-pulse",
    text: "text-emerald-400",
    label: "LIVE",
  },
  STALE: {
    bg: "bg-amber-500/10 border-amber-500/20",
    dot: "bg-amber-400 animate-pulse",
    text: "text-amber-400",
    label: "STALE",
  },
  DISCONNECTED: {
    bg: "bg-rose-500/10 border-rose-500/20",
    dot: "bg-rose-400",
    text: "text-rose-400",
    label: "DISCONNECTED",
  },
};

function formatMs(ms: number): string {
  if (ms < 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

function latencyColor(ms: number): string {
  if (ms < 0) return "text-slate-500";
  if (ms < 5000) return "text-emerald-400";
  if (ms < 15000) return "text-amber-400";
  return "text-rose-400";
}

export function WsHealthPanel({ health }: { health: WsHealth | null }) {
  if (!health) return null;

  const st = STATUS_STYLES[health.ws_status] || STATUS_STYLES.DISCONNECTED;

  return (
    <div className="rounded-xl border border-blue-500/[0.06] bg-[var(--bg-card)] p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-slate-300 tracking-wide uppercase">
          WebSocket & Order Health
        </h3>
        <span
          className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[10px] font-semibold border ${st.bg}`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${st.dot}`} />
          <span className={st.text}>{st.label}</span>
        </span>
      </div>

      {/* Main metrics row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {/* UDS Connection */}
        <div className="rounded-lg bg-[#0a1628] p-2.5">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1">UDS Baglanti</div>
          <div className={`text-sm font-bold ${health.uds_connected ? "text-emerald-400" : "text-rose-400"}`}>
            {health.uds_connected ? "Dual Active" : "Disconnected"}
          </div>
        </div>

        {/* Last WS Event */}
        <div className="rounded-lg bg-[#0a1628] p-2.5">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1">Son WS Event</div>
          <div className={`text-sm font-bold ${latencyColor(health.ws_last_event_ms)}`}>
            {formatMs(health.ws_last_event_ms)}
          </div>
        </div>

        {/* REST Heartbeat */}
        <div className="rounded-lg bg-[#0a1628] p-2.5">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1">REST Heartbeat</div>
          <div className={`text-sm font-bold ${latencyColor(health.heartbeat_age_ms)}`}>
            {formatMs(health.heartbeat_age_ms)}
          </div>
        </div>

        {/* Watchdog */}
        <div className="rounded-lg bg-[#0a1628] p-2.5">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1">Watchdog Timeout</div>
          <div className="text-sm font-bold text-slate-300">
            {health.watchdog_timeout_s}s
          </div>
        </div>
      </div>

      {/* Order state machine summary */}
      <div className="flex items-center gap-4 text-[10px]">
        <span className="text-slate-500">
          Tracked: <span className="text-slate-300 font-semibold">{health.total_tracked_orders}</span>
        </span>
        <span className="text-slate-500">
          Pending: <span className="text-amber-400 font-semibold">{health.pending_orders.length}</span>
        </span>
        <span className="text-slate-500">
          Mismatches:{" "}
          <span className={`font-semibold ${health.mismatches > 0 ? "text-rose-400" : "text-emerald-400"}`}>
            {health.mismatches}
          </span>
        </span>
      </div>

      {/* Pending orders table */}
      {health.pending_orders.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[10px]">
            <thead>
              <tr className="text-slate-500 border-b border-slate-700/50">
                <th className="text-left py-1 pr-3">Order</th>
                <th className="text-left py-1 pr-3">Symbol</th>
                <th className="text-left py-1 pr-3">Type</th>
                <th className="text-left py-1 pr-3">Side</th>
                <th className="text-right py-1 pr-3">Price</th>
                <th className="text-right py-1 pr-3">Age</th>
                <th className="text-right py-1">Last Verify</th>
              </tr>
            </thead>
            <tbody>
              {health.pending_orders.map((o) => (
                <tr key={o.order_id} className="border-b border-slate-800/30 text-slate-300">
                  <td className="py-1 pr-3 font-mono text-[9px]">#{o.order_id}</td>
                  <td className="py-1 pr-3">{o.symbol}</td>
                  <td className="py-1 pr-3">
                    <span
                      className={`px-1.5 py-0.5 rounded text-[9px] font-semibold ${
                        o.type === "TP"
                          ? "bg-teal-500/15 text-teal-400"
                          : o.type === "DCA"
                          ? "bg-amber-500/15 text-amber-400"
                          : "bg-rose-500/15 text-rose-400"
                      }`}
                    >
                      {o.type}
                    </span>
                  </td>
                  <td className={`py-1 pr-3 ${o.side === "BUY" ? "text-teal-400" : "text-rose-400"}`}>
                    {o.side}
                  </td>
                  <td className="py-1 pr-3 text-right font-mono">{o.price}</td>
                  <td className="py-1 pr-3 text-right">{o.age_s.toFixed(0)}s</td>
                  <td className={`py-1 text-right ${latencyColor(o.last_verify_s * 1000)}`}>
                    {o.last_verify_s >= 0 ? `${o.last_verify_s.toFixed(0)}s` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
