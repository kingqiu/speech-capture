export type WorkbenchLayoutSize = "wide" | "compact" | "narrow";

export function workbenchLayoutSize(width: number): WorkbenchLayoutSize {
  if (!Number.isFinite(width) || width <= 0) {
    return "wide";
  }
  if (width <= 860) {
    return "narrow";
  }
  return width <= 1480 ? "compact" : "wide";
}
