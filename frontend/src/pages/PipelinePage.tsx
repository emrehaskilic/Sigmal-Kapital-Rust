import { useState, useEffect, useRef, Fragment } from 'react'
import {
  Play, Square, CheckCircle2, ChevronRight, ChevronDown,
  Lock, Zap, TrendingUp, BarChart3, RotateCcw, ArrowRight,
} from 'lucide-react'
import { usePipelineStore } from '../stores/pipelineStore'
import type { V2Top10Entry } from '../stores/pipelineStore'
import { v2Init, v2GetState, v2StepStart, v2StepSelect, v2Stop, v2Reset, v2UpdateSettings } from '../api'

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT']
const TIMEFRAMES = ['1m', '3m', '5m', '15m']
const TRIAL_OPTIONS = [500, 1000, 2000, 3000]

const STEP_LABELS = ['Adaptive PMax', 'Keltner Channel', 'Graduated DCA', 'Graduated TP']
const STEP_KEYS = ['pmax', 'kc', 'dca', 'tp']
const STEP_DESCRIPTIONS = [
  '9 Adaptive PMax parametresini WF ile optimize eder (saf sinyal, KC/DCA/TP kapalı)',
  '3 Keltner Channel parametresini PMax kilitli olarak optimize eder',
  '4 DCA kademe çarpanını PMax+KC kilitli olarak optimize eder',
  '4 TP kademe oranını PMax+KC+DCA kilitli olarak optimize eder',
]

const PARAM_LABELS: Record<string, string> = {
  vol_lookback: 'Vol Lookback', flip_window: 'Flip Window',
  mult_base: 'Mult Base', mult_scale: 'Mult Scale',
  ma_base: 'MA Base', ma_scale: 'MA Scale',
  atr_base: 'ATR Base', atr_scale: 'ATR Scale',
  update_interval: 'Update Int.',
  kc_length: 'KC Length', kc_multiplier: 'KC Multiplier',
  kc_atr_period: 'KC ATR Period',
  dca_m1: 'DCA Step 1', dca_m2: 'DCA Step 2',
  dca_m3: 'DCA Step 3', dca_m4: 'DCA Step 4',
  tp1: 'TP Step 1', tp2: 'TP Step 2',
  tp3: 'TP Step 3', tp4: 'TP Step 4',
}

// AIBots Dark Theme
const C = {
  card: 'bg-[#0a1628]',
  cardHover: 'bg-[#0f1f35]',
  cardDark: 'bg-[#050a14]',
  border: 'border-blue-500/[0.08]',
  borderActive: 'border-blue-400/30',
  glow: 'shadow-[0_0_20px_rgba(59,130,246,0.04)]',
}

export default function PipelinePage() {
  const store = usePipelineStore()
  const { v2 } = store
  const logEndRef = useRef<HTMLDivElement>(null)

  const [symbol, setSymbol] = useState('ETHUSDT')
  const [timeframe, setTimeframe] = useState('3m')
  const [nTrials, setNTrials] = useState(1000)
  const [selectedRow, setSelectedRow] = useState<number | null>(null)
  const [expandedRow, setExpandedRow] = useState<number | null>(null)

  useEffect(() => {
    store.connect()
    v2GetState().then(data => {
      store.setV2State(data)
    }).catch(() => {})

    const interval = setInterval(() => {
      v2GetState().then(data => {
        store.setV2State(data)
      }).catch(() => {})
    }, 5000)

    return () => {
      store.disconnect()
      clearInterval(interval)
    }
  }, [])

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [store.logs])

  useEffect(() => {
    setSelectedRow(null)
    setExpandedRow(null)
  }, [v2.currentStep])

  const currentStepKey = STEP_KEYS[v2.currentStep] || ''
  const currentTop10 = v2.stepResults[currentStepKey] || []
  const isStepRunning = v2.stepStatus === 'running' || store.running
  const isAwaitingSelection = v2.stepStatus === 'awaiting_selection' && currentTop10.length > 0
  const isPipelineComplete = v2.stepStatus === 'completed'

  const getVisibleTop10 = (): V2Top10Entry[] => {
    if (currentTop10.length > 0) return currentTop10
    for (let i = v2.currentStep - 1; i >= 0; i--) {
      const key = STEP_KEYS[i]
      if (v2.stepResults[key]?.length > 0) return v2.stepResults[key]
    }
    return []
  }

  const handleChangeTimeframe = async (tf: string) => {
    try {
      const res = await v2UpdateSettings({ timeframe: tf })
      if (res.state) store.setV2State(res.state)
    } catch (e: any) {
      store.addLog({ message: `Timeframe hatası: ${e.message}`, level: 'error' })
    }
  }

  const handleChangeTrials = async (n: number) => {
    try {
      const res = await v2UpdateSettings({ n_trials: n })
      if (res.state) store.setV2State(res.state)
    } catch (e: any) {
      store.addLog({ message: `Trial hatası: ${e.message}`, level: 'error' })
    }
  }

  const handleInit = async () => {
    try {
      store.v2Reset()
      store.clearLogs()
      await v2Init(symbol, timeframe, nTrials)
      const data = await v2GetState()
      store.setV2State(data)
    } catch (e: any) {
      store.addLog({ message: `Başlatma hatası: ${e.message}`, level: 'error' })
    }
  }

  const handleStartStep = async () => {
    try {
      setSelectedRow(null)
      setExpandedRow(null)
      store.v2SetStepRunning(currentStepKey)
      await v2StepStart(currentStepKey)
    } catch (e: any) {
      store.addLog({ message: `Adım hatası: ${e.message}`, level: 'error' })
    }
  }

  const handleSelect = async () => {
    if (selectedRow === null) return
    try {
      const res = await v2StepSelect(currentStepKey, selectedRow)
      if (res.state) {
        store.setV2State(res.state)
      }
      setSelectedRow(null)
      setExpandedRow(null)
    } catch (e: any) {
      store.addLog({ message: `Seçim hatası: ${e.message}`, level: 'error' })
    }
  }

  const handleStop = async () => {
    try { await v2Stop() } catch (e: any) {
      store.addLog({ message: `Durdurma hatası: ${e.message}`, level: 'error' })
    }
  }

  const handleReset = async () => {
    try {
      await v2Reset()
      store.v2Reset()
      store.clearLogs()
      setSelectedRow(null)
      setExpandedRow(null)
    } catch (e: any) {
      store.addLog({ message: `Reset hatası: ${e.message}`, level: 'error' })
    }
  }

  const fmtNum = (n: number, dec = 1) => typeof n === 'number' ? n.toFixed(dec) : '—'

  const progress = store.totalTrials > 0 ? Math.round(store.completedTrials / store.totalTrials * 100) : 0

  return (
    <div className="flex flex-col gap-6 max-w-[1400px] mx-auto">

      {/* ===== STEPPER BAR ===== */}
      <div className={`${C.card} rounded-xl border ${C.border} ${C.glow} p-5`}>
        <div className="flex items-center justify-between">
          {STEP_LABELS.map((label, i) => {
            const key = STEP_KEYS[i]
            const isCompleted = v2.selectedIndices[key] !== undefined
            const isActive = v2.active && v2.currentStep === i

            return (
              <Fragment key={key}>
                {i > 0 && (
                  <div className={`flex-1 h-px mx-3 ${isCompleted ? 'bg-emerald-500' : 'bg-blue-500/[0.06]'}`} />
                )}
                <div className="flex items-center gap-2.5">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold
                    ${isCompleted
                      ? 'bg-emerald-500/20 text-emerald-400 ring-1 ring-emerald-500/40'
                      : isActive
                        ? 'bg-blue-500/15 text-blue-400 ring-2 ring-blue-400/50'
                        : 'bg-[#0a1020] text-slate-500 ring-1 ring-blue-500/[0.1]'
                    }`}
                  >
                    {isCompleted ? <CheckCircle2 className="w-4 h-4" /> : i + 1}
                  </div>
                  <div>
                    <div className={`text-sm font-semibold ${
                      isCompleted ? 'text-emerald-400' : isActive ? 'text-blue-400' : 'text-slate-500'
                    }`}>
                      {label}
                    </div>
                    {isActive && v2.stepStatus !== 'idle' && (
                      <div className={`text-[10px] mt-0.5 ${
                        v2.stepStatus === 'running' ? 'text-blue-400' :
                        v2.stepStatus === 'awaiting_selection' ? 'text-amber-400' : 'text-slate-500'
                      }`}>
                        {v2.stepStatus === 'running' ? 'Çalışıyor...' :
                         v2.stepStatus === 'awaiting_selection' ? 'Seçim Bekliyor' : ''}
                      </div>
                    )}
                  </div>
                </div>
              </Fragment>
            )
          })}
        </div>
      </div>

      {/* ===== MAIN GRID ===== */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* SOL PANEL */}
        <div className="space-y-4">

          {/* Pipeline Başlatma */}
          {!v2.active && !isPipelineComplete && (
            <div className={`${C.card} rounded-xl p-5 border ${C.border}`}>
              <h3 className="text-sm font-semibold text-slate-200 mb-4">Pipeline V2 Başlat</h3>

              <div className="mb-3">
                <label className="text-xs text-slate-500 mb-1.5 block">Sembol</label>
                <div className="grid grid-cols-3 gap-1.5">
                  {SYMBOLS.map(s => (
                    <button key={s} onClick={() => setSymbol(s)}
                      className={`px-2 py-1.5 rounded text-xs font-medium transition-all ${
                        symbol === s
                          ? 'bg-blue-500/15 text-blue-400 ring-1 ring-blue-500/30'
                          : `${C.cardHover} text-slate-400 hover:text-slate-300`
                      }`}
                    >{s.replace('USDT', '')}</button>
                  ))}
                </div>
              </div>

              <div className="mb-3">
                <label className="text-xs text-slate-500 mb-1.5 block">Timeframe</label>
                <div className="flex gap-1.5">
                  {TIMEFRAMES.map(tf => (
                    <button key={tf} onClick={() => setTimeframe(tf)}
                      className={`px-3 py-1.5 rounded text-xs font-medium transition-all ${
                        timeframe === tf
                          ? 'bg-blue-500/15 text-blue-400 ring-1 ring-blue-500/30'
                          : `${C.cardHover} text-slate-400 hover:text-slate-300`
                      }`}
                    >{tf}</button>
                  ))}
                </div>
              </div>

              <div className="mb-4">
                <label className="text-xs text-slate-500 mb-1.5 block">Trial Sayısı</label>
                <div className="flex gap-1.5">
                  {TRIAL_OPTIONS.map(n => (
                    <button key={n} onClick={() => setNTrials(n)}
                      className={`px-3 py-1.5 rounded text-xs font-medium transition-all ${
                        nTrials === n
                          ? 'bg-blue-500/15 text-blue-400 ring-1 ring-blue-500/30'
                          : `${C.cardHover} text-slate-400 hover:text-slate-300`
                      }`}
                    >{n.toLocaleString()}</button>
                  ))}
                </div>
              </div>

              <button onClick={handleInit}
                className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-semibold text-sm transition-all"
              >
                <Zap className="w-4 h-4" />Pipeline V2 Başlat
              </button>
            </div>
          )}

          {/* Aktif Adım */}
          {v2.active && (
            <div className={`${C.card} rounded-xl p-5 border ${C.border}`}>
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-slate-200">
                  Adım {v2.currentStep + 1}: {STEP_LABELS[v2.currentStep]}
                </h3>
                <span className={`text-[10px] text-slate-500 ${C.cardHover} px-2 py-0.5 rounded`}>{v2.symbol}</span>
              </div>
              <p className="text-xs text-slate-500 mb-3">{STEP_DESCRIPTIONS[v2.currentStep]}</p>

              {v2.stepStatus === 'idle' && (
                <>
                  <div className="flex gap-3 mb-3">
                    <div className="flex-1">
                      <label className="text-[10px] text-slate-500 mb-1 block">Timeframe</label>
                      <div className="flex gap-1">
                        {TIMEFRAMES.map(tf => (
                          <button key={tf} onClick={() => handleChangeTimeframe(tf)}
                            className={`flex-1 px-1.5 py-1 rounded text-[11px] font-medium transition-all ${
                              v2.timeframe === tf
                                ? 'bg-blue-500/15 text-blue-400 ring-1 ring-blue-500/30'
                                : `${C.cardHover} text-slate-500 hover:text-slate-300`
                            }`}
                          >{tf}</button>
                        ))}
                      </div>
                    </div>
                    <div className="flex-1">
                      <label className="text-[10px] text-slate-500 mb-1 block">Trial</label>
                      <div className="flex gap-1">
                        {TRIAL_OPTIONS.map(n => (
                          <button key={n} onClick={() => handleChangeTrials(n)}
                            className={`flex-1 px-1 py-1 rounded text-[11px] font-medium transition-all ${
                              v2.nTrials === n
                                ? 'bg-blue-500/15 text-blue-400 ring-1 ring-blue-500/30'
                                : `${C.cardHover} text-slate-500 hover:text-slate-300`
                            }`}
                          >{n >= 1000 ? `${n / 1000}K` : n}</button>
                        ))}
                      </div>
                    </div>
                  </div>
                  <button onClick={handleStartStep}
                    className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-semibold text-sm transition-all"
                  ><Play className="w-4 h-4" />Optimizasyonu Başlat</button>
                </>
              )}

              {isStepRunning && (
                <>
                  {store.totalFolds > 0 && (
                    <div className="flex items-center justify-between text-xs text-slate-400 mb-2">
                      <span className="font-medium">Fold {store.currentFold} / {store.totalFolds}</span>
                      <span className="text-[10px]">{store.foldResults.filter(f => f.test_net > 0).length} karlı</span>
                    </div>
                  )}
                  <div className="mb-3">
                    <div className="flex items-center justify-between text-xs text-slate-400 mb-1">
                      <span>Trial {store.completedTrials} / {store.totalTrials}</span>
                      <span>{progress}%</span>
                    </div>
                    <div className="w-full h-2 bg-[#0a1020] rounded-full overflow-hidden">
                      <div className="h-full bg-blue-500 rounded-full transition-all duration-300"
                        style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} />
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-2 mb-3">
                    <div className={`${C.cardHover} rounded-lg p-2 text-center`}>
                      <div className="text-[10px] text-slate-500">Net</div>
                      <div className={`text-sm font-bold ${store.bestOosNet > 0 ? 'text-emerald-400' : 'text-slate-500'}`}>
                        {store.bestOosNet !== 0 ? `${store.bestOosNet > 0 ? '+' : ''}${fmtNum(store.bestOosNet)}%` : '—'}
                      </div>
                    </div>
                    <div className={`${C.cardHover} rounded-lg p-2 text-center`}>
                      <div className="text-[10px] text-slate-500">DD</div>
                      <div className="text-sm font-bold text-amber-400">
                        {store.bestDD > 0 ? `${fmtNum(store.bestDD)}%` : '—'}
                      </div>
                    </div>
                    <div className={`${C.cardHover} rounded-lg p-2 text-center`}>
                      <div className="text-[10px] text-slate-500">Ratio</div>
                      <div className="text-sm font-bold text-blue-400">
                        {store.bestRatio > 0 ? fmtNum(store.bestRatio) : '—'}
                      </div>
                    </div>
                  </div>

                  {store.foldResults.length > 0 && (
                    <div className="flex gap-0.5 mb-3">
                      {store.foldResults.map((f, i) => (
                        <div key={i} className={`flex-1 h-2 rounded-sm ${f.test_net > 0 ? 'bg-emerald-500' : 'bg-red-500'}`}
                          title={`Fold ${f.fold}: ${f.test_net > 0 ? '+' : ''}${f.test_net}%`} />
                      ))}
                    </div>
                  )}

                  <button onClick={handleStop}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-red-600/80 hover:bg-red-600 text-white rounded-lg font-medium text-sm transition-all"
                  ><Square className="w-3.5 h-3.5" />Durdur</button>
                </>
              )}

              {isAwaitingSelection && (
                <button onClick={handleSelect} disabled={selectedRow === null}
                  className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-700 disabled:text-slate-500 text-white rounded-lg font-semibold text-sm transition-all"
                >
                  <ArrowRight className="w-4 h-4" />
                  {selectedRow !== null ? `#${selectedRow + 1} Seç → Sonraki Adım` : 'Tablodan bir sonuç seçin'}
                </button>
              )}
            </div>
          )}

          {/* Tamamlandı */}
          {isPipelineComplete && (
            <div className={`${C.card} rounded-xl p-5 border border-emerald-500/30`}>
              <div className="flex items-center gap-2 mb-3">
                <CheckCircle2 className="w-5 h-5 text-emerald-400" />
                <h3 className="text-sm font-semibold text-emerald-400">Pipeline Tamamlandı</h3>
              </div>
              <p className="text-xs text-slate-400 mb-4">Tüm adımlar tamamlandı.</p>
              <button onClick={handleReset}
                className={`w-full flex items-center justify-center gap-2 px-4 py-2.5 ${C.cardHover} hover:bg-slate-600/30 text-slate-300 rounded-lg font-medium text-sm transition-all`}
              ><RotateCcw className="w-3.5 h-3.5" />Yeni Pipeline Başlat</button>
            </div>
          )}

          {/* Kilitli Parametreler */}
          {Object.keys(v2.lockedParams).length > 0 && (
            <div className={`${C.card} rounded-xl p-5 border ${C.border}`}>
              <h3 className="text-sm font-semibold text-slate-200 mb-3 flex items-center gap-2">
                <Lock className="w-3.5 h-3.5 text-amber-400" />Kilitli Parametreler
              </h3>
              <div className="space-y-3">
                {Object.entries(v2.lockedParams).map(([category, params]) => (
                  <LockedParamSection key={category} category={category} params={params as Record<string, any>} />
                ))}
              </div>
            </div>
          )}

          {(v2.active || isPipelineComplete) && (
            <button onClick={handleReset}
              className="w-full flex items-center justify-center gap-2 px-4 py-2 text-slate-500 hover:text-slate-300 text-xs transition-all"
            ><RotateCcw className="w-3 h-3" />Pipeline Sıfırla</button>
          )}
        </div>

        {/* ORTA + SAĞ PANEL */}
        <div className="lg:col-span-2 space-y-4">

          {(isAwaitingSelection || isPipelineComplete) && (
            <Top10Table
              top10={currentTop10.length > 0 ? currentTop10 : getVisibleTop10()}
              selectedRow={selectedRow} expandedRow={expandedRow}
              onSelect={isAwaitingSelection ? setSelectedRow : undefined}
              onExpand={(i) => setExpandedRow(expandedRow === i ? null : i)}
            />
          )}

          {!isAwaitingSelection && !isPipelineComplete && v2.active && v2.stepStatus !== 'running' && (
            <PreviousStepResults v2={v2} />
          )}

          {isStepRunning && store.foldResults.length > 0 && (
            <div className={`${C.card} rounded-xl p-5 border ${C.border}`}>
              <h3 className="text-sm font-semibold text-slate-200 mb-3 flex items-center gap-2">
                <BarChart3 className="w-4 h-4 text-blue-400" />
                Haftalık Sonuçlar ({store.foldResults.filter(f => f.test_net > 0).length}/{store.foldResults.length} karlı)
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-slate-500 border-b border-blue-500/[0.08]">
                      <th className="text-left py-1.5 px-2 font-medium">Fold</th>
                      <th className="text-left py-1.5 px-2 font-medium">Dönem</th>
                      <th className="text-right py-1.5 px-2 font-medium">Net%</th>
                      <th className="text-right py-1.5 px-2 font-medium">DD%</th>
                      <th className="text-right py-1.5 px-2 font-medium">WR%</th>
                      <th className="text-right py-1.5 px-2 font-medium">Trade</th>
                    </tr>
                  </thead>
                  <tbody>
                    {store.foldResults.map(f => (
                      <tr key={f.fold} className={`border-b border-blue-500/[0.05] ${f.test_net > 0 ? 'bg-emerald-500/5' : 'bg-red-500/5'}`}>
                        <td className="py-1.5 px-2 text-slate-400">{f.fold}</td>
                        <td className="py-1.5 px-2 text-slate-400 text-[10px]">{f.test_period}</td>
                        <td className={`py-1.5 px-2 text-right font-semibold ${f.test_net > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {f.test_net > 0 ? '+' : ''}{fmtNum(f.test_net)}
                        </td>
                        <td className="py-1.5 px-2 text-right text-amber-400">{fmtNum(f.test_dd)}</td>
                        <td className="py-1.5 px-2 text-right text-slate-300">{fmtNum(f.test_wr, 0)}</td>
                        <td className="py-1.5 px-2 text-right text-slate-400">{f.test_trades}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {store.wfSummary?.aggregate && !isStepRunning && (
            <div className={`${C.card} rounded-xl p-5 border border-emerald-500/20`}>
              <h3 className="text-sm font-semibold text-emerald-400 mb-3 flex items-center gap-2">
                <TrendingUp className="w-4 h-4" />Walk-Forward Özet
              </h3>
              <div className="grid grid-cols-4 gap-2">
                <MetricBox label="Karlı Hafta" value={`${store.wfSummary.aggregate.profitable_weeks}/${store.wfSummary.aggregate.total_weeks}`} sub={`${fmtNum(store.wfSummary.aggregate.win_rate_weeks, 0)}%`} />
                <MetricBox label="Toplam Net" value={`${store.wfSummary.aggregate.total_net > 0 ? '+' : ''}${fmtNum(store.wfSummary.aggregate.total_net)}%`} color={store.wfSummary.aggregate.total_net > 0 ? 'emerald' : 'red'} />
                <MetricBox label="Max DD" value={`${fmtNum(store.wfSummary.aggregate.max_dd)}%`} color="amber" />
                <MetricBox label="Toplam Trade" value={`${store.wfSummary.aggregate.total_trades}`} />
              </div>
            </div>
          )}

          {/* Log */}
          <div className={`${C.card} rounded-xl p-4 border ${C.border}`}>
            <div className="flex justify-between items-center mb-2">
              <h3 className="text-xs font-semibold text-slate-400">Log</h3>
              <button onClick={() => store.clearLogs()} className="text-[10px] text-slate-600 hover:text-slate-400 transition-colors">Temizle</button>
            </div>
            <div className={`h-48 overflow-y-auto font-mono text-[11px] space-y-0.5 ${C.cardDark} rounded-lg p-2`}>
              {store.logs.length === 0 ? (
                <p className="text-slate-600">Pipeline başlatıldığında loglar burada görünecek...</p>
              ) : (
                store.logs.map((log, i) => (
                  <div key={i} className={
                    log.level === 'error' ? 'text-red-400'
                    : log.level === 'success' ? 'text-emerald-400'
                    : log.level === 'warn' ? 'text-amber-400'
                    : 'text-slate-500'
                  }>
                    <span className="text-slate-700">[{log.timestamp}]</span> {log.message}
                  </div>
                ))
              )}
              <div ref={logEndRef} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ============================================================
// Sub-Components
// ============================================================

function LockedParamSection({ category, params }: { category: string; params: Record<string, any> }) {
  const [open, setOpen] = useState(false)
  const labels: Record<string, string> = { pmax: 'Adaptive PMax', kc: 'Keltner Channel', dca: 'Graduated DCA', tp: 'Graduated TP' }
  return (
    <div>
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-xs font-medium text-slate-400 hover:text-slate-200 transition-colors w-full"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        <span className="text-amber-400/80">{labels[category] || category}</span>
      </button>
      {open && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 mt-1.5 ml-4">
          {Object.entries(params).map(([k, v]) => (
            <div key={k} className="flex justify-between text-[10px]">
              <span className="text-slate-500">{PARAM_LABELS[k] || k}</span>
              <span className="text-slate-300 font-mono">
                {typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : String(v)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function MetricBox({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  const colorClass = color === 'emerald' ? 'text-emerald-400' : color === 'red' ? 'text-red-400' : color === 'amber' ? 'text-amber-400' : 'text-slate-200'
  return (
    <div className="bg-[#0f1f35] rounded-lg p-2.5 text-center">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`text-base font-bold ${colorClass}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-500">{sub}</div>}
    </div>
  )
}

function Top10Table({ top10, selectedRow, expandedRow, onSelect, onExpand }: {
  top10: V2Top10Entry[]; selectedRow: number | null; expandedRow: number | null;
  onSelect?: (i: number) => void; onExpand: (i: number) => void;
}) {
  if (top10.length === 0) return null
  const fmtNum = (n: number, dec = 1) => typeof n === 'number' ? n.toFixed(dec) : '—'

  return (
    <div className="bg-[#0a1628] rounded-xl p-5 border border-blue-500/[0.08]">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-slate-200 flex items-center gap-2">
          <BarChart3 className="w-4 h-4 text-blue-400" />Top-10 Sonuçlar
        </h3>
        {onSelect && (
          <span className="text-[10px] text-amber-400/80 bg-amber-400/10 px-2 py-0.5 rounded">Bir sonuç seçin</span>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 border-b border-blue-500/[0.08] text-[11px]">
              {onSelect && <th className="w-8 py-2 px-2"></th>}
              <th className="text-left py-2 px-2 font-semibold">#</th>
              <th className="text-right py-2 px-2 font-semibold">Skor</th>
              <th className="text-right py-2 px-2 font-semibold">Net %</th>
              <th className="text-right py-2 px-2 font-semibold">Max DD %</th>
              <th className="text-right py-2 px-2 font-semibold">WR %</th>
              <th className="text-right py-2 px-2 font-semibold">Karlı Fold</th>
              <th className="text-right py-2 px-2 font-semibold">Ratio</th>
              <th className="w-6 py-2 px-2"></th>
            </tr>
          </thead>
          <tbody>
            {top10.map((entry, i) => {
              const isSelected = selectedRow === i
              const isExpanded = expandedRow === i
              const ratio = entry.max_dd > 0 ? (entry.total_net / entry.max_dd).toFixed(1) : '—'
              return (
                <Fragment key={entry.tid ?? i}>
                  <tr onClick={() => onSelect?.(i)}
                    className={`border-b border-blue-500/[0.05] transition-all cursor-pointer ${
                      isSelected ? 'bg-blue-500/15 ring-1 ring-blue-500/30'
                      : i === 0 ? 'bg-emerald-500/5 hover:bg-emerald-500/10'
                      : 'hover:bg-white/[0.03]'
                    }`}
                  >
                    {onSelect && (
                      <td className="py-2 px-2">
                        <div className={`w-4 h-4 rounded-full border-2 flex items-center justify-center ${
                          isSelected ? 'border-blue-500 bg-blue-500' : 'border-slate-600'
                        }`}>
                          {isSelected && <div className="w-1.5 h-1.5 rounded-full bg-white" />}
                        </div>
                      </td>
                    )}
                    <td className="py-2 px-2 text-slate-400 font-semibold">{i + 1}</td>
                    <td className="py-2 px-2 text-right text-blue-400 font-mono">{fmtNum(entry.score, 2)}</td>
                    <td className={`py-2 px-2 text-right font-bold ${entry.total_net > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {entry.total_net > 0 ? '+' : ''}{fmtNum(entry.total_net)}
                    </td>
                    <td className="py-2 px-2 text-right text-amber-400">{fmtNum(entry.max_dd)}</td>
                    <td className="py-2 px-2 text-right text-slate-300">{fmtNum(entry.avg_wr, 0)}</td>
                    <td className="py-2 px-2 text-right text-slate-400">
                      {entry.profitable_weeks ?? '—'}
                    </td>
                    <td className={`py-2 px-2 text-right font-semibold ${Number(ratio) > 1 ? 'text-blue-400' : 'text-slate-500'}`}>
                      {ratio}
                    </td>
                    <td className="py-2 px-2">
                      <button onClick={(e) => { e.stopPropagation(); onExpand(i) }}
                        className="text-slate-500 hover:text-slate-300 transition-colors"
                      >
                        {isExpanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                      </button>
                    </td>
                  </tr>
                  {isExpanded && (
                    <tr className="bg-[#050a14]/50">
                      <td colSpan={onSelect ? 9 : 8} className="px-4 py-3">
                        <div className="flex gap-6">
                          <div className="flex-1">
                            <div className="text-[10px] text-slate-500 font-semibold mb-1.5 uppercase tracking-wider">Parametreler</div>
                            <div className="grid grid-cols-3 gap-x-4 gap-y-0.5">
                              {Object.entries(entry.params).map(([k, v]) => (
                                <div key={k} className="flex justify-between text-[10px]">
                                  <span className="text-slate-500">{PARAM_LABELS[k] || k}</span>
                                  <span className="text-slate-300 font-mono">
                                    {typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : String(v)}
                                  </span>
                                </div>
                              ))}
                            </div>
                          </div>
                          {entry.folds && entry.folds.length > 0 && (
                            <div className="flex-1">
                              <div className="text-[10px] text-slate-500 font-semibold mb-1.5 uppercase tracking-wider">Haftalık Sonuçlar</div>
                              <div className="flex gap-0.5 mb-1">
                                {entry.folds.map((f: any, fi: number) => (
                                  <div key={fi} className={`flex-1 h-2 rounded-sm ${f.test_net > 0 ? 'bg-emerald-500' : 'bg-red-500'}`}
                                    title={`Fold ${f.fold}: ${f.test_net > 0 ? '+' : ''}${f.test_net}%`} />
                                ))}
                              </div>
                              <div className="grid grid-cols-4 gap-1 text-[9px]">
                                {entry.folds.map((f: any, fi: number) => (
                                  <div key={fi} className={`text-center ${f.test_net > 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                    {f.test_net > 0 ? '+' : ''}{f.test_net}%
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function PreviousStepResults({ v2 }: { v2: any }) {
  const completedSteps = STEP_KEYS.filter(k => v2.selectedIndices[k] !== undefined)
  if (completedSteps.length === 0) return null
  return (
    <div className="bg-[#0a1628] rounded-xl p-5 border border-blue-500/[0.08]">
      <h3 className="text-sm font-semibold text-slate-200 mb-3">Tamamlanan Adımlar</h3>
      <div className="space-y-2">
        {completedSteps.map(key => {
          const stepIdx = STEP_KEYS.indexOf(key)
          const selectedIdx = v2.selectedIndices[key]
          const top10 = v2.stepResults[key] || []
          const selected = top10[selectedIdx]
          if (!selected) return null
          return (
            <div key={key} className="bg-[#0f1f35] rounded-lg p-3">
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                  <span className="text-xs font-medium text-slate-300">{STEP_LABELS[stepIdx]}</span>
                </div>
                <span className="text-[10px] text-slate-500">#{selectedIdx + 1} seçildi</span>
              </div>
              <div className="flex gap-4 text-[10px] text-slate-400">
                <span>Net: <span className={selected.total_net > 0 ? 'text-emerald-400' : 'text-red-400'}>
                  {selected.total_net > 0 ? '+' : ''}{selected.total_net?.toFixed(1)}%
                </span></span>
                <span>DD: <span className="text-amber-400">{selected.max_dd?.toFixed(1)}%</span></span>
                <span>WR: <span className="text-slate-300">{selected.avg_wr?.toFixed(0)}%</span></span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
