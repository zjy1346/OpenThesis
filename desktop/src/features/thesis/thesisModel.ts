export type JsonObject = Record<string, unknown>;

export type ThesisDraft = {
  coreThesis: string;
  keyAssumptions: string;
  supportingEvidence: string;
  counterEvidence: string;
  invalidationConditions: string;
  leadingIndicators: string;
  changeReason: string;
};

export type ThesisDiffEntry = {
  path: string;
  kind: "added" | "removed" | "changed";
  before?: unknown;
  after?: unknown;
};

const FIELD_KEYS = {
  keyAssumptions: ["key_assumptions", "assumptions"],
  supportingEvidence: ["supporting_evidence_ids", "supporting_evidence"],
  counterEvidence: ["counter_evidence_ids", "counter_evidence", "contradicting_evidence_ids", "contradicting_evidence"],
  invalidationConditions: ["invalidation_conditions"],
  leadingIndicators: ["leading_indicators"],
} as const;

function hasOwn(value: JsonObject, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function clone<T>(value: T): T {
  if (value === undefined) return value;
  return JSON.parse(JSON.stringify(value)) as T;
}

function compactJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  if (typeof value === "object" && !Array.isArray(value)) {
    const object = value as JsonObject;
    if (object.value !== undefined && object.concept) return `${object.concept}: ${object.value} ${object.unit || object.currency || ''} · ${object.end_date || object.fiscal_year || ''}`;
    const sourceId = ["source_id", "evidence_id", "id"].find((key) => typeof object[key] === "string");
    if (sourceId) {
      const detail = ["text", "description", "label"].find((key) => typeof object[key] === "string");
      return detail ? `${object[sourceId]} — ${object[detail]}` : String(object[sourceId]);
    }
    const prose = ['text', 'description', 'summary', 'thesis', 'condition', 'indicator', 'assumption', 'reason', 'title'];
    return prose.filter(key => typeof object[key] === 'string').map(key => object[key]).join(' · ');
  }
  if (typeof value === "object") return compactJson(value);
  return String(value);
}

function displayList(value: unknown): string {
  if (!Array.isArray(value)) return displayValue(value);
  return value.map(displayValue).filter(Boolean).join("\n");
}

function firstKey(content: JsonObject, keys: readonly string[]): string {
  return keys.find((key) => hasOwn(content, key)) ?? keys[0];
}

function firstValue(content: JsonObject, keys: readonly string[]): unknown {
  const key = keys.find((candidate) => hasOwn(content, candidate));
  return key ? content[key] : undefined;
}

function claimValues(content: JsonObject, keys: readonly string[]): unknown[] {
  if (!Array.isArray(content.claims)) return [];
  const values: unknown[] = [];
  for (const claim of content.claims) {
    if (!claim || typeof claim !== "object" || Array.isArray(claim)) continue;
    const value = firstValue(claim as JsonObject, keys);
    if (Array.isArray(value)) values.push(...value);
    else if (value !== undefined) values.push(value);
  }
  return values;
}

function firstValueOrClaims(content: JsonObject, keys: readonly string[]): unknown {
  const direct = firstValue(content, keys);
  return direct === undefined ? claimValues(content, keys) : direct;
}

function coreText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const object = value as JsonObject;
    for (const key of ["text", "summary", "analysis", "conclusion"]) {
      if (typeof object[key] === "string") return object[key] as string;
    }
  }
  return displayValue(value);
}

export function readThesisDraft(content: JsonObject): ThesisDraft {
  return {
    coreThesis: coreText(content.thesis),
    keyAssumptions: displayList(firstValueOrClaims(content, FIELD_KEYS.keyAssumptions)),
    supportingEvidence: displayList(firstValueOrClaims(content, FIELD_KEYS.supportingEvidence)),
    counterEvidence: displayList(firstValueOrClaims(content, FIELD_KEYS.counterEvidence)),
    invalidationConditions: displayList(firstValue(content, FIELD_KEYS.invalidationConditions)),
    leadingIndicators: displayList(firstValue(content, FIELD_KEYS.leadingIndicators)),
    changeReason: typeof content.change_reason === "string" ? content.change_reason : "",
  };
}

function parseEditedLines(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

/**
 * Update only the human-editable string entries. Objects, numbers and other
 * legacy values stay in their original position so an old thesis can make a
 * structured edit without silently dropping data it does not understand.
 */
export function updateMixedArray(original: unknown, edited: string[]): unknown[] {
  const source = Array.isArray(original) ? clone(original) : [];
  let editedIndex = 0;
  const updated = source.flatMap((item) => {
    if (typeof item !== "string") {
      if (edited[editedIndex] === displayValue(item)) editedIndex += 1;
      return [item];
    }
    if (editedIndex >= edited.length) {
      return [];
    }
    const next = edited[editedIndex];
    editedIndex += 1;
    return next ? [next] : [];
  });
  return [...updated, ...edited.slice(editedIndex)];
}

function updateCore(original: unknown, value: string): unknown {
  if (original && typeof original === "object" && !Array.isArray(original)) {
    const result = clone(original) as JsonObject;
    const key = ["text", "summary", "analysis", "conclusion"].find((candidate) => typeof result[candidate] === "string") ?? "text";
    result[key] = value;
    return result;
  }
  return value;
}

export function applyThesisDraft(content: JsonObject, draft: ThesisDraft, initial: ThesisDraft): JsonObject {
  const next = clone(content);
  if (draft.coreThesis !== initial.coreThesis) next.thesis = updateCore(content.thesis, draft.coreThesis);

  const listFields: Array<keyof Pick<ThesisDraft, "keyAssumptions" | "supportingEvidence" | "counterEvidence" | "invalidationConditions" | "leadingIndicators">> = [
    "keyAssumptions", "supportingEvidence", "counterEvidence", "invalidationConditions", "leadingIndicators",
  ];
  for (const field of listFields) {
    if (draft[field] === initial[field]) continue;
    const keys = FIELD_KEYS[field];
    const key = firstKey(content, keys);
    const original = hasOwn(content, key) ? content[key] : undefined;
    const edited = parseEditedLines(draft[field]);
    next[key] = Array.isArray(original) ? updateMixedArray(original, edited) : edited;
  }
  if (draft.changeReason !== initial.changeReason) next.change_reason = draft.changeReason;
  return next;
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as JsonObject).sort().map((key) => `${JSON.stringify(key)}:${stable((value as JsonObject)[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function itemIdentity(value: unknown): string {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const item = value as JsonObject;
    const key = ['claim_id', 'evidence_id', 'assumption_id', 'source_id', 'id'].find(name => typeof item[name] === 'string');
    if (key) return `${key}:${item[key]}`;
  }
  return stable(value);
}

function collectKeys(previous: JsonObject, current: JsonObject): string[] {
  return Array.from(new Set([...Object.keys(previous), ...Object.keys(current)])).sort();
}

export function diffThesisVersions(previous: JsonObject, current: JsonObject): ThesisDiffEntry[] {
  const changes: ThesisDiffEntry[] = [];
  for (const key of collectKeys(previous, current)) {
    if (key.startsWith('_')) continue;
    const beforeExists = hasOwn(previous, key);
    const afterExists = hasOwn(current, key);
    if (!beforeExists && afterExists) changes.push({ path: key, kind: "added", after: clone(current[key]) });
    else if (beforeExists && !afterExists) changes.push({ path: key, kind: "removed", before: clone(previous[key]) });
    else if (stable(previous[key]) !== stable(current[key])) {
      const beforeArray = previous[key];
      const afterArray = current[key];
      if (Array.isArray(beforeArray) && Array.isArray(afterArray)) {
        const beforeCount = changes.length;
        const beforeItems = new Map(beforeArray.map((item) => [itemIdentity(item), item]));
        const afterItems = new Map(afterArray.map((item) => [itemIdentity(item), item]));
        if (beforeItems.size !== beforeArray.length || afterItems.size !== afterArray.length) {
          changes.push({ path: key, kind: 'changed', before: clone(beforeArray), after: clone(afterArray) });
          continue;
        }
        for (const [identity, item] of afterItems) {
          if (!beforeItems.has(identity)) changes.push({ path: `${key}[]`, kind: "added", after: clone(item) });
          else if (stable(beforeItems.get(identity)) !== stable(item)) changes.push({ path: `${key}[]`, kind: 'changed', before: clone(beforeItems.get(identity)), after: clone(item) });
        }
        for (const [identity, item] of beforeItems) {
          if (!afterItems.has(identity)) changes.push({ path: `${key}[]`, kind: "removed", before: clone(item) });
        }
        if (changes.length === beforeCount) {
          changes.push({ path: key, kind: "changed", before: clone(beforeArray), after: clone(afterArray) });
        }
      } else {
        changes.push({ path: key, kind: "changed", before: clone(previous[key]), after: clone(current[key]) });
      }
    }
  }
  return changes;
}

export function formatDiffValue(value: unknown): string {
  return displayValue(value);
}
