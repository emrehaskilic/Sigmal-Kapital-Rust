import { useState, useRef, useEffect, type ReactNode } from "react";

interface CollapsibleProps {
  title: string;
  summary?: string;
  defaultCollapsed?: boolean;
  children: ReactNode;
  className?: string;
}

export function Collapsible({
  title,
  summary,
  defaultCollapsed = false,
  children,
  className = "",
}: CollapsibleProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const contentRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | "auto">("auto");

  useEffect(() => {
    if (!contentRef.current) return;
    if (collapsed) {
      setHeight(contentRef.current.scrollHeight);
      requestAnimationFrame(() => setHeight(0));
    } else {
      setHeight(contentRef.current.scrollHeight);
      const onEnd = () => setHeight("auto");
      contentRef.current.addEventListener("transitionend", onEnd, { once: true });
    }
  }, [collapsed]);

  return (
    <div
      className={`bg-card rounded-2xl shadow-card transition-all ${
        collapsed ? "border-b-2 border-card" : ""
      } ${className}`}
    >
      <button
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between px-5 py-3.5 group cursor-pointer select-none"
      >
        <div className="flex items-center gap-3 min-w-0">
          <h2 className={`text-xs font-semibold tracking-tight transition-colors ${
            collapsed ? "text-muted" : "text-secondary"
          }`}>
            {title}
          </h2>
          {collapsed && summary && (
            <span className="text-[10px] text-faint font-mono truncate">
              {summary}
            </span>
          )}
        </div>
        <svg
          className={`w-3.5 h-3.5 text-muted transition-transform duration-200 group-hover:text-secondary ${
            collapsed ? "" : "rotate-180"
          }`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      <div
        ref={contentRef}
        className="overflow-hidden transition-[height] duration-250 ease-in-out"
        style={{ height: collapsed ? 0 : height }}
      >
        <div className="px-5 pb-5">
          {children}
        </div>
      </div>
    </div>
  );
}
