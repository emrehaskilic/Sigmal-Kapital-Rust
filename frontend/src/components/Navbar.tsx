export type PageId = "dashboard" | "backtest" | "live" | "pipeline";

interface NavbarProps {
  page: PageId;
  setPage: (p: PageId) => void;
}

export function Navbar({ page, setPage }: NavbarProps) {
  const tab = (id: PageId, label: string, extraClass?: string) => (
    <button
      onClick={() => setPage(id)}
      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
        page === id
          ? id === "live"
            ? "bg-emerald-500/15 text-emerald-400 border border-emerald-500/25"
            : id === "pipeline"
              ? "bg-blue-500/15 text-blue-400 border border-blue-500/20"
              : "bg-sky-500/15 text-sky-400"
          : "text-slate-400 hover:text-slate-200 hover:bg-blue-500/10"
      } ${extraClass || ""}`}
    >
      {label}
    </button>
  );

  return (
    <nav className="bg-[#0a1628]/90 border-b border-blue-500/[0.08] px-4 md:px-6 py-2">
      <div className="max-w-7xl mx-auto flex items-center gap-4">
        <div className="flex items-center gap-2 mr-2">
          <img src="/logo.svg" alt="Sigma Kapital" className="h-7" />
        </div>
        <div className="flex gap-1">
          {tab("dashboard", "Dry-Run")}
          {tab("backtest", "Backtest")}
          {tab("pipeline", "Optimization")}
          {tab("live", "LIVE")}
        </div>
      </div>
    </nav>
  );
}
