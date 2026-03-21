import { useEffect, useRef, useState } from "react";
import { useTheme } from "../theme";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type SeriesMarker,
} from "lightweight-charts";

interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  pmax: number | null;
  mavg: number | null;
  kc_upper: number | null;
  kc_lower: number | null;
  direction: number;
}

interface MarkerData {
  time: number;
  position: string;
  color: string;
  shape: string;
  text: string;
  price: number;
}

interface GridLevel {
  price: number;
  label: string;
  filled: boolean;
}

interface ConfigFlags {
  pct_stop_enabled: boolean;
  filters_enabled: boolean;
  dyncomp_enabled: boolean;
}

interface ChartDataResponse {
  candles: Candle[];
  markers: MarkerData[];
  grid_levels: GridLevel[];
  live_markers?: MarkerData[];
  config_flags?: ConfigFlags;
  error?: string;
}

interface Props {
  symbol: string;
  botRunning: boolean;
  /** Chart title override */
  title?: string;
  /** "dryrun" = all markers same style, "live" = past signals dimmed, live trades highlighted */
  mode?: "dryrun" | "live";
  /** Live trade markers to overlay (from live executor) */
  liveMarkers?: MarkerData[];
}

const BASE = `http://${window.location.hostname}:8000`;

export function PMaxChart({ symbol, botRunning, title, mode = "dryrun", liveMarkers }: Props) {
  const { theme } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const pmaxRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcUpperRef = useRef<ISeriesApi<"Line"> | null>(null);
  const kcLowerRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [loading, setLoading] = useState(true);
  const [configFlags, setConfigFlags] = useState<ConfigFlags>({
    pct_stop_enabled: false,
    filters_enabled: false,
    dyncomp_enabled: false,
  });
  const pollRef = useRef<number | null>(null);
  const priceLinesRef = useRef<any[]>([]);

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: theme === "dark" ? "#040810" : "#ffffff" },
        textColor: theme === "dark" ? "#64748b" : "#94a3b8",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#1e293b18" },
        horzLines: { color: "#1e293b18" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1e293b40" },
      timeScale: {
        borderColor: "#1e293b40",
        timeVisible: true,
        secondsVisible: false,
      },
      width: containerRef.current.clientWidth,
      height: 500,
    });

    const candle = chart.addCandlestickSeries({
      upColor: "#2dd4bf",
      downColor: "#f43f5e",
      borderUpColor: "#2dd4bf",
      borderDownColor: "#f43f5e",
      wickUpColor: "#2dd4bf50",
      wickDownColor: "#f43f5e50",
    });

    const pmax = chart.addLineSeries({
      color: "#f43f5e",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const kcUpper = chart.addLineSeries({
      color: "#f59e0b40",
      lineWidth: 1,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    const kcLower = chart.addLineSeries({
      color: "#f59e0b40",
      lineWidth: 1,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    chartRef.current = chart;
    candleRef.current = candle;
    pmaxRef.current = pmax;
    kcUpperRef.current = kcUpper;
    kcLowerRef.current = kcLower;

    const onResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      if (pollRef.current) clearInterval(pollRef.current);
      chart.remove();
    };
  }, [theme]);

  // Fetch + render
  const fetchData = async () => {
    const cs = candleRef.current;
    const ps = pmaxRef.current;
    const ku = kcUpperRef.current;
    const kl = kcLowerRef.current;
    if (!cs || !ps) return;

    try {
      const res = await fetch(`${BASE}/api/chart-data?symbol=${symbol}&limit=1500&source=${mode}`);
      const data: ChartDataResponse = await res.json();
      if (data.error) return;

      // Candles
      cs.setData(
        data.candles.map((c) => ({
          time: c.time as Time,
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
        }))
      );

      // PMax
      const pmaxPts = data.candles
        .filter((c) => c.pmax != null)
        .map((c) => ({ time: c.time as Time, value: c.pmax! }));
      ps.setData(pmaxPts);

      const lastDir = data.candles[data.candles.length - 1]?.direction ?? 0;
      ps.applyOptions({ color: lastDir === 1 ? "#2dd4bf" : "#f43f5e" });

      // Keltner Channel bands
      if (ku) {
        const kcUpperPts = data.candles
          .filter((c) => c.kc_upper != null)
          .map((c) => ({ time: c.time as Time, value: c.kc_upper! }));
        ku.setData(kcUpperPts);
      }
      if (kl) {
        const kcLowerPts = data.candles
          .filter((c) => c.kc_lower != null)
          .map((c) => ({ time: c.time as Time, value: c.kc_lower! }));
        kl.setData(kcLowerPts);
      }

      // Remove old price lines
      for (const pl of priceLinesRef.current) {
        try { cs.removePriceLine(pl); } catch { /* already removed */ }
      }
      priceLinesRef.current = [];

      // Grid levels as price lines (AVG entry, DCA, TP, STOP)
      if (data.grid_levels && data.grid_levels.length > 0) {
        for (const gl of data.grid_levels) {
          const isStop = gl.label.includes("STOP");
          const isDCA = gl.label.startsWith("DCA");
          const isTP = gl.label === "TP";
          const isAvg = gl.label.startsWith("AVG");

          let color = "#475569";  // default slate
          let lineStyle = 2;     // dashed
          if (isStop) {
            color = gl.filled ? "#f43f5e" : "#f43f5e40";
            lineStyle = 0;
          } else if (isDCA) {
            color = "#2dd4bf60";
          } else if (isTP) {
            color = "#38bdf860";
          } else if (isAvg) {
            color = "#fbbf24";
            lineStyle = 2;       // dashed for avg
          }

          const pl = cs.createPriceLine({
            price: gl.price,
            color,
            lineWidth: 1,
            lineStyle,
            axisLabelVisible: true,
            title: gl.label,
          });
          priceLinesRef.current.push(pl);
        }
      }

      // Config flags
      if (data.config_flags) {
        setConfigFlags(data.config_flags);
      }

      // Markers — process based on mode
      const allRawMarkers = [...data.markers];

      // In live mode, dim past signals and add live trade markers
      if (mode === "live") {
        // Dim all dry-run/past markers (reduce opacity via color)
        for (let i = 0; i < allRawMarkers.length; i++) {
          const m = allRawMarkers[i];
          allRawMarkers[i] = {
            ...m,
            color: m.color.length === 7 ? m.color + "40" : m.color,
            text: m.text ? `[${m.text}]` : "",
          };
        }
        // Append live trade markers from backend (bright, prominent)
        const serverLiveMarkers = data.live_markers || [];
        for (const lm of serverLiveMarkers) {
          allRawMarkers.push(lm);
        }
        // Also append any prop-based live markers
        if (liveMarkers && liveMarkers.length > 0) {
          for (const lm of liveMarkers) {
            allRawMarkers.push(lm);
          }
        }
      }

      if (allRawMarkers.length > 0) {
        // Shorten TP labels: "TP +18.24" → "+18"
        const shortenedMarkers = allRawMarkers.map((m) => {
          let text = m.text;
          if (text.startsWith("TP +") || text.startsWith("TP -")) {
            const val = parseFloat(text.replace("TP ", ""));
            text = val >= 0 ? `+${Math.round(val)}` : `${Math.round(val)}`;
          }
          return { ...m, text };
        });

        // Thin markers: skip TP markers that are too close in time (< 3 candles apart)
        const MIN_GAP = 180 * 3; // 3 candles × 180s (3m)
        const filtered: typeof shortenedMarkers = [];
        let lastTpTime = 0;
        for (const m of shortenedMarkers) {
          const isTP = m.shape === "circle" && (m.text.startsWith("+") || m.text.startsWith("-") || m.text.startsWith("[+") || m.text.startsWith("[-"));
          if (isTP && m.time - lastTpTime < MIN_GAP) {
            continue;
          }
          if (isTP) lastTpTime = m.time;
          filtered.push(m);
        }

        // Sort by time (required by lightweight-charts)
        filtered.sort((a, b) => a.time - b.time);

        const markers: SeriesMarker<Time>[] = filtered.map((m) => ({
          time: m.time as Time,
          position: m.position as "aboveBar" | "belowBar" | "inBar",
          color: m.color,
          shape: m.shape as "circle" | "arrowUp" | "arrowDown" | "square",
          text: m.text,
        }));
        cs.setMarkers(markers);
      } else {
        cs.setMarkers([]);
      }

      setLoading(false);
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    fetchData();
    // Always poll — chart works even without bot running
    pollRef.current = window.setInterval(fetchData, botRunning ? 10000 : 30000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [symbol, botRunning]);

  return (
    <div className="bg-card rounded-2xl shadow-card p-5">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-xs font-semibold text-slate-300 tracking-tight">{title || `${symbol} - Trend Inventory`}</h2>
        <div className="flex items-center gap-3 text-[9px] text-slate-500/60">
          <span className="flex items-center gap-1">
            <span className="w-3 h-0.5 bg-teal-400 inline-block rounded-full" /> Trend
          </span>
          <span className="flex items-center gap-1">
            <span className="w-3 h-0.5 bg-amber-400/40 inline-block rounded-full" /> Channel
          </span>
          <span className="text-teal-400/80">&#9650; DCA</span>
          <span className="text-sky-400/80">&#9679; TP</span>
          <span className="text-amber-400/80">&#9632; REV</span>
          {configFlags.pct_stop_enabled && (
            <span className="text-rose-400/80">&#9632; STOP</span>
          )}
        </div>
      </div>
      {loading && (
        <div className="text-slate-500/60 text-xs text-center py-6">Grafik yukleniyor...</div>
      )}
      <div ref={containerRef} style={{ width: "100%", height: 500 }} />
    </div>
  );
}
