import { useEffect, useRef, useState } from "react";
import { defaultSidebarWidth, DEFAULT_PANEL_WIDTH, panelLimits, sidebarLimits } from "./panel-width";
import { t } from "./i18n";

export default function PanelResizer({ width, viewport, change, save, side = "right", sidebarWidth, panelOpen = true, bounds, label, defaultWidth }: {
  width: number; viewport: number; change: (value: number) => void; save: (value: number) => void;
  side?: "left" | "right"; sidebarWidth?: number; panelOpen?: boolean;
  bounds?: { min: number; max: number }; label?: string; defaultWidth?: number;
}) {
  const drag = useRef<{ x: number; width: number; current: number } | null>(null);
  const [resizing, setResizing] = useState(false);
  const left = side === "left";
  const limits = bounds || (left ? sidebarLimits(viewport, panelOpen) : panelLimits(viewport, sidebarWidth));
  const clamp = (value: number) => Math.round(Math.max(limits.min, Math.min(limits.max, value)));
  useEffect(() => {
    if (resizing) document.body.classList.add("panel-resizing");
    return () => document.body.classList.remove("panel-resizing");
  }, [resizing]);
  function finish() {
    if (!drag.current) return;
    save(drag.current.current); drag.current = null; setResizing(false);
  }
  return <div className={"panel-resizer " + (left ? "sidebar-resizer " : "") + (resizing ? "dragging" : "")} role="separator"
    aria-label={label || (left ? t("调整历史栏宽度") : t("调整右侧面板宽度"))} aria-orientation="vertical" aria-valuemin={limits.min} aria-valuemax={limits.max} aria-valuenow={width}
    title={t("拖动调整宽度，双击恢复默认宽度；方向键也可调整")} tabIndex={0}
    onPointerDown={(event) => {
      if (event.button !== 0) return;
      event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { x: event.clientX, width, current: width }; setResizing(true);
    }}
    onPointerMove={(event) => {
      if (!drag.current) return;
      const value = clamp(drag.current.width + (left ? event.clientX - drag.current.x : drag.current.x - event.clientX));
      drag.current.current = value; change(value);
    }} onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={finish}
    onDoubleClick={() => { const value = clamp(defaultWidth ?? (left ? defaultSidebarWidth(viewport) : DEFAULT_PANEL_WIDTH)); change(value); save(value); }}
    onKeyDown={(event) => {
      const value = event.key === "ArrowLeft" ? width + (left ? -24 : 24) : event.key === "ArrowRight" ? width + (left ? 24 : -24) :
        event.key === "Home" ? limits.min : event.key === "End" ? limits.max : null;
      if (value === null) return;
      event.preventDefault(); const next = clamp(value); change(next); save(next);
    }} />;
}
