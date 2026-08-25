import { describe, expect, it } from "vitest";

import { buildSummaryChanges, countSummaryChanges } from "../src/summary-diff";

describe("summary diff", () => {
  it("builds readable ordered changes and keeps evidence references", () => {
    const changes = buildSummaryChanges(
      {
        title: "产品访谈",
        summary: { text: "旧总览", evidence: ["seg_1"] },
        decisions: [],
        actions: [{ task: "保持不变", evidence: ["seg_same"] }],
        risks: [{ text: "旧风险", evidence: ["seg_old"] }]
      },
      {
        title: "产品访谈",
        summary: { text: "新总览", evidence: ["seg_1", "seg_2"] },
        decisions: [{ text: "九月上线", evidence: ["seg_3"] }],
        actions: [{ task: "保持不变", evidence: ["seg_same"] }],
        risks: []
      }
    );

    expect(changes.map((change) => change.id)).toEqual([
      "summary",
      "decisions",
      "risks"
    ]);
    expect(changes[0]).toMatchObject({
      label: "一分钟总览",
      kind: "modified",
      beforeText: "旧总览",
      afterText: "新总览",
      evidenceIds: ["seg_1", "seg_2"]
    });
    expect(changes[1]).toMatchObject({
      kind: "added",
      afterText: "• 九月上线"
    });
    expect(changes[2]).toMatchObject({
      kind: "removed",
      beforeText: "• 旧风险",
      evidenceIds: [],
      deletionBasisMissing: true
    });
    expect(countSummaryChanges(changes)).toEqual({
      added: 1,
      modified: 1,
      removed: 1
    });
  });

  it("hides derived chapters and uses user-facing labels for timeline sections", () => {
    const changes = buildSummaryChanges(
      {
        chapters: [{ title: "旧派生章节" }],
        timeline_sections: [{ title: "开场" }]
      },
      {
        chapters: [],
        timeline_sections: [{ title: "项目背景" }]
      }
    );

    expect(changes).toHaveLength(1);
    expect(changes[0]).toMatchObject({
      id: "timeline_sections",
      label: "按时间顺序摘要",
      kind: "modified"
    });
  });

  it("ignores key order and unchanged nested content", () => {
    expect(
      buildSummaryChanges(
        { summary: { evidence: ["seg_1"], text: "相同" } },
        { summary: { text: "相同", evidence: ["seg_1"] } }
      )
    ).toEqual([]);
  });
});
