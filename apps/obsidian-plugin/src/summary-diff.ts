export type SummaryChangeKind = "added" | "modified" | "removed";

export interface SummaryChangeBlock {
  readonly id: string;
  readonly label: string;
  readonly kind: SummaryChangeKind;
  readonly beforeText: string;
  readonly afterText: string;
  readonly evidenceIds: readonly string[];
  readonly deletionBasisMissing: boolean;
}

export interface SummaryChangeCounts {
  readonly added: number;
  readonly modified: number;
  readonly removed: number;
}

const SECTION_ORDER = [
  "title",
  "summary",
  "context",
  "highlights",
  "topics",
  "discussion_threads",
  "speaker_summaries",
  "decisions",
  "actions",
  "risks",
  "open_questions",
  "timeline_sections",
  "scene_sections"
] as const;

const SECTION_LABELS: Readonly<Record<string, string>> = {
  title: "标题",
  summary: "一分钟总览",
  context: "背景与目的",
  highlights: "核心信息",
  topics: "重点议题",
  discussion_threads: "讨论脉络",
  speaker_summaries: "参与者观点",
  decisions: "明确决定",
  actions: "后续事项",
  risks: "风险与注意",
  open_questions: "待确认问题",
  timeline_sections: "按时间顺序摘要",
  scene_sections: "内容章节"
};

const HIDDEN_DERIVED_KEYS = new Set(["chapters"]);

export function buildSummaryChanges(
  before: Readonly<Record<string, unknown>> | null,
  after: Readonly<Record<string, unknown>> | null
): readonly SummaryChangeBlock[] {
  const beforeDocument = before ?? {};
  const afterDocument = after ?? {};
  const keys = orderedKeys(beforeDocument, afterDocument);
  const changes: SummaryChangeBlock[] = [];
  for (const key of keys) {
    const beforeValue = beforeDocument[key];
    const afterValue = afterDocument[key];
    if (stableValue(beforeValue) === stableValue(afterValue)) {
      continue;
    }
    const beforeText = readableValue(beforeValue);
    const afterText = readableValue(afterValue);
    if (!beforeText && !afterText) {
      continue;
    }
    const kind = !beforeText ? "added" : !afterText ? "removed" : "modified";
    changes.push({
      id: key,
      label: SECTION_LABELS[key] ?? humanizeKey(key),
      kind,
      beforeText,
      afterText,
      evidenceIds: kind === "removed" ? [] : collectEvidence(afterValue),
      deletionBasisMissing: kind === "removed"
    });
  }
  return changes;
}

export function countSummaryChanges(
  changes: readonly SummaryChangeBlock[]
): SummaryChangeCounts {
  return changes.reduce<SummaryChangeCounts>(
    (counts, change) => ({
      added: counts.added + (change.kind === "added" ? 1 : 0),
      modified: counts.modified + (change.kind === "modified" ? 1 : 0),
      removed: counts.removed + (change.kind === "removed" ? 1 : 0)
    }),
    { added: 0, modified: 0, removed: 0 }
  );
}

export function renderSummaryCandidateMarkdown(
  document: Readonly<Record<string, unknown>> | null
): string {
  if (!document) {
    return "";
  }
  const title = readableValue(document.title) || "候选笔记";
  const lines = [`# ${title}`];
  const keys = orderedKeys(document, document).filter((key) => key !== "title");
  for (const key of keys) {
    const text = readableValue(document[key]);
    if (!text) {
      continue;
    }
    lines.push(
      "",
      `## ${SECTION_LABELS[key] ?? humanizeKey(key)}`,
      "",
      text.replace(/^• /gm, "- ")
    );
  }
  return `${lines.join("\n").trim()}\n`;
}

function orderedKeys(
  before: Readonly<Record<string, unknown>>,
  after: Readonly<Record<string, unknown>>
): readonly string[] {
  const found = new Set([...Object.keys(before), ...Object.keys(after)]);
  for (const key of HIDDEN_DERIVED_KEYS) {
    found.delete(key);
  }
  const ordered = SECTION_ORDER.filter((key) => found.delete(key));
  return [...ordered, ...[...found].sort()];
}

function readableValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value.trim();
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => readableItem(item))
      .filter(Boolean)
      .map((item) => `• ${item}`)
      .join("\n");
  }
  return readableItem(value);
}

function readableItem(value: unknown): string {
  if (!isRecord(value)) {
    return readableValue(value);
  }
  const title = firstText(value, "display_name", "title", "name");
  const body = firstText(value, "text", "summary", "task", "question", "risk");
  if (title && body && title !== body) {
    return `${title}：${body}`;
  }
  if (body || title) {
    return body || title;
  }
  const nested = Object.entries(value)
    .filter(([key]) => !["evidence", "speaker_id", "kind", "status"].includes(key))
    .map(([key, item]) => {
      const text = readableValue(item);
      return text ? `${SECTION_LABELS[key] ?? humanizeKey(key)}：${text}` : "";
    })
    .filter(Boolean);
  return nested.join("；");
}

function firstText(value: Readonly<Record<string, unknown>>, ...keys: string[]): string {
  for (const key of keys) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  return "";
}

function collectEvidence(value: unknown): readonly string[] {
  const found = new Set<string>();
  const visit = (item: unknown): void => {
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (!isRecord(item)) {
      return;
    }
    const evidence = item.evidence;
    if (Array.isArray(evidence)) {
      for (const evidenceId of evidence) {
        if (typeof evidenceId === "string") {
          found.add(evidenceId);
        }
      }
    }
    Object.values(item).forEach(visit);
  };
  visit(value);
  return [...found];
}

function stableValue(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableValue).join(",")}]`;
  }
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableValue(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

function humanizeKey(value: string): string {
  return value.replaceAll("_", " ");
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
