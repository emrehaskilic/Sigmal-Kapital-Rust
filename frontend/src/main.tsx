import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import BacktestPage from './pages/BacktestPage.tsx'
import LivePage from './pages/LivePage.tsx'
import PipelinePage from './pages/PipelinePage.tsx'
import MonitorPage from './pages/MonitorPage.tsx'
import { Navbar } from './components/Navbar.tsx'
import type { PageId } from './components/Navbar.tsx'
import { ThemeProvider } from './theme.tsx'

function Dashboard() {
  const [page, setPage] = useState<PageId>("dashboard");

  return (
    <ThemeProvider>
      <Navbar page={page} setPage={setPage} />
      <div style={{ display: page === "dashboard" ? "block" : "none" }}>
        <App />
      </div>
      <div style={{ display: page === "backtest" ? "block" : "none" }}>
        <BacktestPage />
      </div>
      <div style={{ display: page === "pipeline" ? "block" : "none" }}>
        <PipelinePage />
      </div>
      <div style={{ display: page === "live" ? "block" : "none" }}>
        <LivePage />
      </div>
    </ThemeProvider>
  );
}

function Root() {
  // Route: /sigmakapital/* → MonitorPage (standalone, no navbar)
  // Everything else → Dashboard (normal app with navbar)
  const isMonitor = window.location.pathname.startsWith("/sigmakapital");

  if (isMonitor) {
    return <MonitorPage />;
  }

  return <Dashboard />;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
