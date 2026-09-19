export type WorkbenchLayoutSize = "wide" | "compact" | "narrow";

export function workbenchLayoutSize(width: number): WorkbenchLayoutSize {
  if (!Number.isFinite(width) || width <= 0) {
    return "wide";
  }
  if (width <= 1_040) {
    return "narrow";
  }
  return width <= 1_280 ? "compact" : "wide";
}
