import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import BacktestPage from './pages/BacktestPage.tsx'
import LivePage from './pages/LivePage.tsx'
import PipelinePage from './pages/PipelinePage.tsx'
import { Navbar } from './components/Navbar.tsx'
import type { PageId } from './components/Navbar.tsx'

function Root() {
  const [page, setPage] = useState<PageId>("dashboard");

  return (
    <>
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
    </>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
