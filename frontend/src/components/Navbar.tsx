import { useTheme } from "../theme";

export type PageId = "dashboard" | "backtest" | "live" | "pipeline";

interface NavbarProps {
  page: PageId;
  setPage: (p: PageId) => void;
}

export function Navbar({ page, setPage }: NavbarProps) {
  const { theme, toggle } = useTheme();

  const tab = (id: PageId, label: string) => (
    <button
      onClick={() => setPage(id)}
      className={`px-3.5 py-1.5 rounded-xl text-[11px] font-medium transition-all ${
        page === id
          ? id === "live"
            ? "bg-teal-500/10 text-teal-400 ring-1 ring-teal-500/15"
            : id === "pipeline"
              ? "bg-blue-500/10 text-blue-400 ring-1 ring-blue-500/15"
              : "bg-sky-500/10 text-sky-400 ring-1 ring-sky-500/15"
          : "text-secondary hover:text-primary hover:bg-black/5 dark:hover:bg-white/5"
      }`}
    >
      {label}
    </button>
  );

  return (
    <nav className="bg-nav border-b border-nav px-4 md:px-6 py-2.5 backdrop-blur-sm">
      <div className="max-w-7xl mx-auto flex items-center gap-5">
        <div className="flex items-center shrink-0 mr-1">
          <img
            src="/logo.svg"
            alt="Sigma Kapital"
            className="h-10 w-auto object-contain"
            draggable={false}
          />
        </div>
        <div className="flex gap-1.5">
          {tab("dashboard", "Dry-Run")}
          {tab("backtest", "Backtest")}
          {tab("pipeline", "Optimization")}
          {tab("live", "LIVE")}
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Theme toggle */}
        <button
          onClick={toggle}
          className="relative w-9 h-9 rounded-xl bg-black/5 dark:bg-white/5 hover:bg-black/10 dark:hover:bg-white/10 flex items-center justify-center transition-all"
          title={theme === "dark" ? "Light Mode" : "Dark Mode"}
        >
          {/* Sun icon */}
          <svg
            className={`w-4 h-4 absolute transition-all duration-300 ${
              theme === "dark" ? "opacity-0 rotate-90 scale-0" : "opacity-100 rotate-0 scale-100"
            }`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <circle cx="12" cy="12" r="5" />
            <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
          </svg>
          {/* Moon icon */}
          <svg
            className={`w-4 h-4 absolute transition-all duration-300 ${
              theme === "dark" ? "opacity-100 rotate-0 scale-100" : "opacity-0 -rotate-90 scale-0"
            }`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
          </svg>
        </button>
      </div>
    </nav>
  );
}
