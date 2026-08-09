import { describe, expect, it } from "vitest";

import { BACKEND_METHODS } from "./protocol";

describe("platform-neutral backend contract", () => {
  it("exposes one stable method name for each supported workflow", () => {
    expect(new Set(BACKEND_METHODS).size).toBe(BACKEND_METHODS.length);
    expect(BACKEND_METHODS).toEqual(expect.arrayContaining([
      "app.bootstrap",
      "models.discover",
      "research.start",
      "research.delete",
      "research.get_report",
      "settings.update",
    ]));
  });
});
