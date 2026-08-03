import { describe, expect, it } from "vitest";

import { workbenchLayoutSize } from "../src/workbench-layout";

describe("workbench layout sizing", () => {
  it("uses the workbench pane width rather than the application window width", () => {
    expect(workbenchLayoutSize(1_586)).toBe("wide");
    expect(workbenchLayoutSize(1_480)).toBe("compact");
    expect(workbenchLayoutSize(1_120)).toBe("compact");
    expect(workbenchLayoutSize(860)).toBe("narrow");
  });

  it("does not collapse before a measurable width is available", () => {
    expect(workbenchLayoutSize(0)).toBe("wide");
    expect(workbenchLayoutSize(Number.NaN)).toBe("wide");
  });
});
