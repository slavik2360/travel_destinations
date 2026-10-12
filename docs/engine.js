import DATA from "./data.js";

// Точный порт RecommendationEngine / AISimulatedEngine / build_absurd_note из MyProgram/main.py.
// Критерии - от наименее важного к самому важному (см. комментарий в Database.find_relaxed
// в main.py: "тип отдыха" отпускается в последнюю очередь).
const CRITERIA_ORDER = [
  ["интересы и впечатления", "interest"],
  ["продолжительность поездки", "duration"],
  ["визовые предпочтения", "visa"],
  ["время года", "season"],
  ["бюджет", "budget"],
  ["континент", "continent"],
  ["тип отдыха", "type"],
];
const WILDCARD_FIELDS = new Set(["season", "duration", "visa", "interest"]);

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}
function randInt(min, max) {
  return min + Math.floor(Math.random() * (max - min + 1));
}
function format(template, values) {
  return template.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? `{${key}}`);
}

// "Мале (виллы)" -> "Мале"; "Ушуая + круиз к островам" -> "Ушуая" - только для текста шутки.
export function cleanPlaceName(text) {
  return text.split("(")[0].split("+")[0].trim();
}

/**
 * Механизм ослабления условий: последовательно убираем самые незначимые из РЕАЛЬНО заданных
 * пользователем критериев (значение "any" = "не важно" вообще не участвует в фильтрации -
 * ослаблять нечего), пока не найдётся совпадение.
 */
export function findRelaxed(destinations, query) {
  const active = CRITERIA_ORDER.filter(([, field]) => query[field] !== "any");
  const trace = [];
  let lastRows = [];
  let lastDropped = [];

  for (let cut = 0; cut <= active.length; cut++) {
    const remaining = active.slice(cut);
    const dropped = active.slice(0, cut).map(([name]) => name);

    const rows = destinations.filter((dest) =>
      remaining.every(([, field]) => {
        const val = query[field];
        if (WILDCARD_FIELDS.has(field)) return dest[field] === val || dest[field] === "any";
        return dest[field] === val;
      })
    );

    let stage;
    if (cut === 0) stage = "Точное совпадение по всем указанным вами критериям";
    else if (cut === active.length) stage = "Любое направление из базы знаний (остальные критерии тоже не учитываются)";
    else stage = "Без учёта: " + dropped.join(", ");

    trace.push({ stage, count: rows.length, success: rows.length > 0 });
    lastRows = rows;
    lastDropped = dropped;
    if (rows.length > 0) return { rows, dropped, trace };
  }
  return { rows: lastRows, dropped: lastDropped, trace };
}

function ruleReasoning(relaxed) {
  const n = relaxed.length;
  const relaxedStr = relaxed.join(", ");
  let variants;
  if (n === 0) variants = DATA.RULE_NOTES.PERFECT;
  else if (n <= 2) variants = DATA.RULE_NOTES.MINOR;
  else if (n <= 4) variants = DATA.RULE_NOTES.MODERATE;
  else variants = DATA.RULE_NOTES.MAJOR;
  return format(pick(variants), { relaxed: relaxedStr });
}

export function recommendRule(destinations, query) {
  const { rows, dropped, trace } = findRelaxed(destinations, query);
  const dest = pick(rows);
  return {
    country: dest.country,
    city: dest.city,
    description: dest.description,
    reasoning: ruleReasoning(dropped),
    mode: "🧭 Экспертная система (правила)",
    trace,
    relaxed: dropped,
  };
}

function aiClosingNote(relaxed) {
  const n = relaxed.length;
  const relaxedStr = relaxed.join(", ");
  let variants;
  if (n === 0) variants = DATA.AI_NOTES.PERFECT;
  else if (n <= 2) variants = DATA.AI_NOTES.MINOR;
  else if (n <= 4) variants = DATA.AI_NOTES.MODERATE;
  else variants = DATA.AI_NOTES.MAJOR;
  return format(pick(variants), { relaxed: relaxedStr });
}

function nameOf(table, code, anyLabel) {
  if (code === "any") return anyLabel;
  const row = DATA[table].find((r) => r[0] === code);
  return row ? row[1] : code;
}

export function recommendAI(destinations, query) {
  const { rows, dropped, trace } = findRelaxed(destinations, query);
  const confidence = Math.max(40, 97 - dropped.length * 8 - randInt(0, 6));
  const dest = pick(rows);

  const names = {
    budget: nameOf("BUDGETS", query.budget, "любой"),
    continent: nameOf("CONTINENTS", query.continent, "любой"),
    type: nameOf("REST_TYPES", query.type, "любой"),
    season: nameOf("SEASONS", query.season, "любой"),
    duration: nameOf("DURATIONS", query.duration, "любая"),
    visa: nameOf("VISAS", query.visa, "любой"),
    interest: nameOf("INTERESTS", query.interest, "любой"),
  };
  const intro = format(pick(DATA.AI_PHRASES_INTRO), names);
  const note = aiClosingNote(dropped);
  const reasoning = `${intro} направление «${dest.country} — ${dest.city}».${note} Уверенность модели: ${confidence}%.`;

  return {
    country: dest.country,
    city: dest.city,
    description: dest.description,
    reasoning,
    mode: "🤖 ИИ-режим",
    confidence,
    trace,
    relaxed: dropped,
  };
}

export function buildAbsurdNote(continent, type, duration, relaxed, result) {
  const combo = DATA.ABSURD_COMBOS[`${continent}|${type}`];
  const ctx = { country: result.country, city: cleanPlaceName(result.city) };

  if (combo && relaxed.includes("континент")) {
    return format(pick(combo), ctx);
  }
  if (
    continent === "antarctica" &&
    (type === "nature" || type === "active") &&
    duration === "short" &&
    relaxed.includes("продолжительность поездки")
  ) {
    return format(pick(DATA.ANTARCTICA_DURATION_JOKES), ctx);
  }
  return null;
}

export function applyAbsurdNote(result, continent, type, duration) {
  const note = buildAbsurdNote(continent, type, duration, result.relaxed, result);
  if (note) result.reasoning = note + " " + result.reasoning;
  return result;
}
