import { create } from 'zustand'

export interface PipelineJob {
  id: string
  symbol: string
  strategy: string
  timeframes: string[]
  status: 'queued' | 'downloading' | 'running' | 'completed' | 'failed'
  current_round: number
  current_timeframe: string | null
  total_rounds: number
  selected_params_per_round: Record<string, any>
  selected_metrics_per_round: Record<string, any>
  final_result: any
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface TrialData {
  tid: number
  score: number
  is_net: number
  oos_net: number
  wr: number
  dd: number
  ratio: number
  trades: number
  params?: Record<string, any>
  folds?: any[]
}

export interface FoldResult {
  fold: number
  train_period: string
  test_period: string
  test_net: number
  test_dd: number
  test_wr: number
  test_trades: number
  ratio: number
  params?: Record<string, any>
}

export interface WalkForwardSummary {
  total_folds: number
  train_days: number
  test_days: number
  step_days: number
  trials_per_fold: number
  aggregate?: {
    total_weeks: number
    profitable_weeks: number
    win_rate_weeks: number
    avg_weekly_net: number
    median_weekly_net: number
    best_week_net: number
    worst_week_net: number
    total_net: number
    avg_dd: number
    max_dd: number
    avg_wr: number
    total_trades: number
    consistency_score: number
  }
}

export interface LogEntry {
  message: string
  level: string
  timestamp?: string
}

// ============================================================
// V2 Pipeline Types
// ============================================================

export interface V2StepDef {
  key: string
  label: string
  strategy: string
}

export interface V2Top10Entry {
  tid: number
  score: number
  total_net: number
  max_dd: number
  avg_wr: number
  profitable_weeks?: number
  worst_week?: number
  best_week?: number
  balance?: number
  total_trades?: number
  params: Record<string, any>
  folds?: any[]
  // Uyumluluk
  oos_net: number
  dd: number
  wr: number
  is_net: number
}

export type V2StepStatus = 'idle' | 'running' | 'awaiting_selection' | 'completed'

export interface V2State {
  active: boolean
  currentStep: number
  stepStatus: V2StepStatus
  symbol: string
  timeframe: string
  lockedParams: Record<string, any>
  stepResults: Record<string, V2Top10Entry[]>
  selectedIndices: Record<string, number>
  nTrials: number
  steps: V2StepDef[]
}

interface PipelineState {
  // Connection
  ws: WebSocket | null
  connected: boolean

  // Pipeline V1 status
  running: boolean
  queue: PipelineJob[]
  currentJob: PipelineJob | null

  // Current round/fold info
  currentRound: number
  currentRoundName: string
  totalTrials: number
  completedTrials: number
  bestScore: number
  bestOosNet: number
  bestDD: number
  bestRatio: number

  // Top trials for current round
  top10: TrialData[]

  // Walk-Forward state
  isWalkForward: boolean
  currentFold: number
  totalFolds: number
  foldResults: FoldResult[]
  wfSummary: WalkForwardSummary | null

  // Logs
  logs: LogEntry[]

  // V2 Pipeline State
  v2: V2State

  // Actions
  connect: () => void
  disconnect: () => void
  setQueue: (queue: PipelineJob[]) => void
  addLog: (log: LogEntry) => void
  clearLogs: () => void
  reset: () => void
  restoreWalkForwardState: (state: any) => void

  // V2 Actions
  setV2State: (state: any) => void
  v2SetStepRunning: (stepKey: string) => void
  v2SetStepResults: (stepKey: string, top10: V2Top10Entry[]) => void
  v2SelectResult: (stepKey: string, index: number, lockedParams: Record<string, any>, nextStep: number) => void
  v2Reset: () => void
}

const DEFAULT_V2: V2State = {
  active: false,
  currentStep: 0,
  stepStatus: 'idle',
  symbol: 'ETHUSDT',
  timeframe: '3m',
  lockedParams: {},
  stepResults: {},
  selectedIndices: {},
  nTrials: 1000,
  steps: [
    { key: 'pmax_discovery', label: 'PMax Keşif', strategy: 'unified_pmax' },
    { key: 'kc_optimize', label: 'KC Optimize', strategy: 'unified_kc' },
    { key: 'kelly_dyncomp', label: 'Kelly DynComp', strategy: 'unified_kelly' },
    { key: 'dynsl_test', label: 'DynSL Test', strategy: 'unified_dynsl' },
  ],
}

export const usePipelineStore = create<PipelineState>((set, get) => ({
  ws: null,
  connected: false,
  running: false,
  queue: [],
  currentJob: null,
  currentRound: 0,
  currentRoundName: '',
  totalTrials: 0,
  completedTrials: 0,
  bestScore: 0,
  bestOosNet: 0,
  bestDD: 0,
  bestRatio: 0,
  top10: [],
  isWalkForward: false,
  currentFold: 0,
  totalFolds: 0,
  foldResults: [],
  wfSummary: null,
  logs: [],
  v2: { ...DEFAULT_V2 },

  connect: () => {
    const ws = new WebSocket('ws://localhost:8055/ws/pipeline')

    ws.onopen = () => {
      set({ ws, connected: true })
    }

    ws.onclose = () => {
      set({ ws: null, connected: false })
      setTimeout(() => {
        if (!get().connected) get().connect()
      }, 3000)
    }

    ws.onerror = () => {
      set({ connected: false })
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        const { type, data } = msg

        switch (type) {
          case 'pipeline_status': {
            const isRunning = !!(data.running ?? data.pipeline_running ?? false)
            const currentState = get()
            set({
              running: isRunning || currentState.isWalkForward,
              queue: data.queue ?? [],
              currentJob: data.current ?? null,
            })
            break
          }

          // === Walk-Forward Events ===
          case 'walkforward_started':
            set({
              isWalkForward: true,
              running: true,
              totalFolds: data.total_folds,
              currentFold: 0,
              foldResults: [],
              wfSummary: {
                total_folds: data.total_folds,
                train_days: data.train_days,
                test_days: data.test_days,
                step_days: data.step_days,
                trials_per_fold: data.trials_per_fold,
              },
            })
            get().addLog({
              message: `Walk-Forward basliyor: ${data.total_folds} fold, ${data.trials_per_fold} trial/fold`,
              level: 'info',
            })
            break

          case 'fold_started':
            set({
              currentFold: data.fold,
              currentRound: data.fold,
              currentRoundName: `Fold ${data.fold}`,
              totalTrials: data.n_trials,
              completedTrials: 0,
              bestScore: 0,
              bestOosNet: 0,
              bestDD: 0,
              bestRatio: 0,
              top10: [],
            })
            break

          case 'fold_completed':
            set(s => ({
              foldResults: [...s.foldResults, data.result],
              top10: data.top5 ?? [],
            }))
            get().addLog({
              message: `Fold ${data.fold} tamamlandi — Test: ${data.result.test_net > 0 ? '+' : ''}${data.result.test_net}% DD:${data.result.test_dd}% [${data.result.test_period}]`,
              level: data.result.test_net > 0 ? 'success' : 'warn',
            })
            break

          case 'walkforward_completed':
            if (data.summary?.aggregate) {
              const agg = data.summary.aggregate
              set(s => ({
                wfSummary: { ...s.wfSummary!, aggregate: agg },
                running: false,
              }))
              get().addLog({
                message: `Walk-Forward tamamlandi! ${agg.profitable_weeks}/${agg.total_weeks} hafta karli, Ort: ${agg.avg_weekly_net > 0 ? '+' : ''}${agg.avg_weekly_net}%`,
                level: 'success',
              })
            }
            // V2: top10 varsa step'i güncelle
            if (data.summary?.top10) {
              const step = data.summary.step
              const stepKeyMap: Record<string, string> = {
                pmax: 'pmax_discovery',
                kc: 'kc_optimize',
                kelly: 'kelly_dyncomp',
                dynsl: 'dynsl_test',
              }
              const stepKey = stepKeyMap[step]
              if (stepKey) {
                get().v2SetStepResults(stepKey, data.summary.top10)
              }
            }
            break

          // === Standard Pipeline Events ===
          case 'round_started':
            set({
              currentRound: data.round,
              currentRoundName: data.name,
              totalTrials: data.n_trials,
              completedTrials: 0,
              bestScore: 0,
              bestOosNet: 0,
              bestDD: 0,
              bestRatio: 0,
              top10: [],
            })
            break

          case 'trial_completed':
            set(s => {
              const newCompleted = s.completedTrials + 1
              return {
                completedTrials: newCompleted,
                bestScore: Math.max(s.bestScore, data.score ?? 0),
                bestOosNet: data.oos_net > s.bestOosNet ? data.oos_net : s.bestOosNet,
                bestDD: data.dd < s.bestDD || s.bestDD === 0 ? data.dd : s.bestDD,
                bestRatio: Math.max(s.bestRatio, data.ratio ?? 0),
              }
            })
            break

          case 'round_completed':
            set({
              top10: data.top10 ?? [],
              currentRound: data.round,
            })
            get().addLog({
              message: `Round ${data.round} (${data.name}) tamamlandi — Secilen: OOS=${data.selected?.oos_net}% DD=${data.selected?.dd}%`,
              level: 'success',
            })
            break

          case 'pair_completed':
            get().addLog({
              message: `${data.symbol} tamamlandi!`,
              level: 'success',
            })
            break

          // === V2 Events ===
          case 'v2_step_started':
            set(s => ({
              running: true,
              isWalkForward: true,
              v2: { ...s.v2, stepStatus: 'running' },
            }))
            get().addLog({
              message: `V2 Adım başlıyor: ${data.label}`,
              level: 'info',
            })
            break

          case 'v2_step_completed':
            set(s => ({
              running: false,
              v2: {
                ...s.v2,
                stepStatus: 'awaiting_selection',
                stepResults: { ...s.v2.stepResults, [data.step]: data.top10 },
              },
            }))
            get().addLog({
              message: `V2 ${data.step} tamamlandı — top-10 hazır, seçim yapın`,
              level: 'success',
            })
            break

          case 'v2_selection_made':
            set(s => ({
              v2: {
                ...s.v2,
                lockedParams: data.locked_params,
                currentStep: data.next_step,
                stepStatus: 'idle',
                selectedIndices: { ...s.v2.selectedIndices, [data.step]: data.selected_index },
              },
            }))
            break

          case 'log':
            get().addLog(data)
            break

          case 'error':
            get().addLog({ message: data.message, level: 'error' })
            break
        }
      } catch (e) {
        console.error('WS parse error:', e)
      }
    }

    set({ ws })
  },

  disconnect: () => {
    const { ws } = get()
    if (ws) ws.close()
    set({ ws: null, connected: false })
  },

  setQueue: (queue) => set({ queue }),

  addLog: (log) => set(s => ({
    logs: [...s.logs.slice(-299), { ...log, timestamp: new Date().toLocaleTimeString() }]
  })),

  clearLogs: () => set({ logs: [] }),

  reset: () => set({
    running: false,
    currentRound: 0,
    currentRoundName: '',
    totalTrials: 0,
    completedTrials: 0,
    bestScore: 0,
    bestOosNet: 0,
    bestDD: 0,
    bestRatio: 0,
    top10: [],
    isWalkForward: false,
    currentFold: 0,
    totalFolds: 0,
    foldResults: [],
    wfSummary: null,
  }),

  restoreWalkForwardState: (s: any) => set(prev => ({
    running: s.running ?? true,
    isWalkForward: true,
    currentFold: s.current_fold ?? 0,
    totalFolds: s.total_folds ?? 0,
    completedTrials: Math.max(prev.completedTrials, s.completed_trials ?? 0),
    totalTrials: s.total_trials ?? 1000,
    bestOosNet: Math.max(prev.bestOosNet, s.best_oos_net ?? 0),
    bestDD: s.best_dd > 0 ? (prev.bestDD === 0 ? s.best_dd : Math.min(prev.bestDD, s.best_dd)) : prev.bestDD,
    bestRatio: Math.max(prev.bestRatio, s.best_ratio ?? 0),
    currentRound: s.current_fold ?? 0,
    currentRoundName: `Fold ${s.current_fold ?? 0}`,
    foldResults: (s.fold_results ?? []).length >= prev.foldResults.length
      ? (s.fold_results ?? []).map((f: any) => ({
          fold: f.fold,
          train_period: f.train_period ?? '',
          test_period: f.test_period ?? '',
          test_net: f.test_net ?? 0,
          test_dd: f.test_dd ?? 0,
          test_wr: f.test_wr ?? 0,
          test_trades: f.test_trades ?? 0,
          ratio: f.ratio ?? 0,
          params: f.params,
        }))
      : prev.foldResults,
    wfSummary: {
      total_folds: s.total_folds ?? 0,
      train_days: 90,
      test_days: 7,
      step_days: 7,
      trials_per_fold: s.total_trials ?? 1000,
      aggregate: s.aggregate ?? prev.wfSummary?.aggregate,
    },
  })),

  // ============================================================
  // V2 Actions
  // ============================================================

  setV2State: (state: any) => set(s => ({
    v2: {
      ...s.v2,
      active: state.active ?? false,
      currentStep: state.current_step ?? 0,
      stepStatus: state.step_status ?? 'idle',
      symbol: state.symbol ?? 'ETHUSDT',
      timeframe: state.timeframe ?? '3m',
      lockedParams: state.locked_params ?? {},
      stepResults: state.step_results ?? {},
      selectedIndices: state.selected_indices ?? {},
      nTrials: state.n_trials ?? 1000,
      steps: state.steps ?? s.v2.steps,
    },
  })),

  v2SetStepRunning: (_stepKey: string) => set(s => ({
    v2: { ...s.v2, stepStatus: 'running' },
    running: true,
    isWalkForward: true,
    completedTrials: 0,
    totalTrials: s.v2.nTrials,
    bestOosNet: 0,
    bestDD: 0,
    bestRatio: 0,
    foldResults: [],
  })),

  v2SetStepResults: (stepKey: string, top10: V2Top10Entry[]) => set(s => ({
    v2: {
      ...s.v2,
      stepStatus: 'awaiting_selection',
      stepResults: { ...s.v2.stepResults, [stepKey]: top10 },
    },
    running: false,
  })),

  v2SelectResult: (stepKey, index, lockedParams, nextStep) => set(s => ({
    v2: {
      ...s.v2,
      lockedParams,
      currentStep: nextStep,
      stepStatus: nextStep >= s.v2.steps.length ? 'completed' : 'idle',
      selectedIndices: { ...s.v2.selectedIndices, [stepKey]: index },
      active: nextStep < s.v2.steps.length,
    },
  })),

  v2Reset: () => set({
    v2: { ...DEFAULT_V2 },
    running: false,
    isWalkForward: false,
    completedTrials: 0,
    totalTrials: 0,
    bestOosNet: 0,
    bestDD: 0,
    bestRatio: 0,
    foldResults: [],
    wfSummary: null,
  }),
}))
