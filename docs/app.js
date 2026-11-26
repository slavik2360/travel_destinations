import DATA from "./data.js";
import { recommendRule, applyAbsurdNote } from "./engine.js";
import { describeRate, ensureRate, formatKzt, formatUsd, getSettings } from "./ai.js";
import { compareWithQuery, countMatches, emptyFilters, estimateCost, parseWish, recommendSmart } from "./smart.js";
import { costCardHtml, createAiScreens, escapeHtml, wireCostCard } from "./ai-ui.js";
import { fetchDestinationImageUrl } from "./wiki.js";
import { loadDestinations, makeDestinationId } from "./destinations.js";
import {
  getHistory, addHistoryEntry, updateHistoryEntry, clearHistory,
  addCustomDestination, removeCustomDestination,
  deleteBuiltinDestination,
} from "./storage.js";

const root = document.getElementById("app");

const CATEGORIES = [
  { code: "budget", title: "Бюджет", table: "BUDGETS" },
  { code: "continent", title: "Континент", table: "CONTINENTS" },
  { code: "type", title: "Тип отдыха", table: "REST_TYPES" },
  { code: "season", title: "Время года", table: "SEASONS" },
  { code: "duration", title: "Продолжительность поездки", table: "DURATIONS" },
  { code: "visa", title: "Визовые предпочтения", table: "VISAS" },
  { code: "interest", title: "Интересы и впечатления", table: "INTERESTS" },
];

const NAMES = {};
for (const cat of CATEGORIES) {
  NAMES[cat.code] = Object.fromEntries(DATA[cat.table].map((r) => [r[0], r[1]]));
  NAMES[cat.code].any = cat.code === "duration" ? "любая" : "любой";
}

const state = {
  screen: "menu",
  useAi: false,
  wizard: null,
  lastResult: null,
  kbFilter: "",
  travelersSample: null,
  wizardUseAi: false, // режим текущего подбора: меню ИИ запускает его независимо от галочки
  query: null, // запрос подбора, меняется уточнениями
  filters: emptyFilters(),
  resultAnimated: null,
};

function freshWizard() {
  return { catIndex: 0, optIndex: 0, answers: {}, historyStack: [] };
}

function go(screen) {
  state.screen = screen;
  render();
}

function startWizard(useAi) {
  state.wizardUseAi = useAi;
  go("gate");
}

const aiScreens = createAiScreens({ state, go, startWizard });

const SCREEN_RENDERERS = {
  menu: renderMenu, gate: renderGate, known: renderKnown, farewell: renderFarewell, travelers: renderTravelers,
  wizard: renderWizard, thinking: renderThinking, result: renderResult, explanation: renderExplanation,
  history: renderHistory, kb: renderKb, ...aiScreens.renderers,
};
const SCREEN_WIRERS = {
  menu: wireMenu, gate: wireGate, known: wireKnown, farewell: wireFarewell, travelers: wireTravelers,
  wizard: wireWizard, thinking: wireThinking, result: wireResult, explanation: wireExplanation,
  history: wireHistory, kb: wireKb, ...aiScreens.wirers,
};

function render() {
  root.innerHTML = SCREEN_RENDERERS[state.screen]();
  SCREEN_WIRERS[state.screen]();
  window.scrollTo(0, 0);
}

// ---------------- Меню ----------------
function renderMenu() {
  return `
    <div class="icon-xl">${state.useAi ? "🤖" : "✈️"}</div>
    <h1 class="title">Выбор направления для путешествия</h1>
    <p class="subtitle">Ответьте на несколько вопросов Да / Нет — и получите готовую рекомендацию</p>
    <div class="toggle-row">
      <label for="ai-toggle">🤖 Использовать ИИ</label>
      <label class="switch"><input type="checkbox" id="ai-toggle" ${state.useAi ? "checked" : ""}><span class="slider"></span></label>
    </div>
    <p class="subtitle">${aiHint()}</p>
    <button class="btn" id="start-btn">Начать подбор путешествия</button>
    <button class="btn secondary" id="ai-menu-btn">🤖 Меню ИИ и консультант</button>
    <button class="btn secondary" id="history-btn">История подборов</button>
    <button class="btn secondary" id="kb-btn">База знаний (направления)</button>
    <footer class="hint">История и добавленные направления хранятся только на этом устройстве. Подбор работает в браузере; сообщения ИИ-консультанту обрабатываются на внешнем сервере.</footer>
  `;
}

function aiHint() {
  return state.useAi
    ? "Режим ИИ включён: сравню все направления и найду лучший компромисс"
    : "ИИ сравнит с вашими ответами все направления сразу, объяснит выбор и учтёт ваши уточнения";
}

function wireMenu() {
  document.getElementById("ai-menu-btn").addEventListener("click", () => go("aiMenu"));
  document.getElementById("ai-toggle").addEventListener("change", (e) => {
    state.useAi = e.target.checked;
    render();
  });
  document.getElementById("start-btn").addEventListener("click", () => startWizard(state.useAi));
  document.getElementById("history-btn").addEventListener("click", () => { state.screen = "history"; render(); });
  document.getElementById("kb-btn").addEventListener("click", () => { state.screen = "kb"; render(); });
}

// ---------------- Входная развилка (см. MyProgram/shema.html) ----------------
function renderGate() {
  return `
    <div class="icon-xl">✈️</div>
    <div class="question-box"><div class="q-text">Хотели выбрать направление для путешествия?</div></div>
    <div class="row">
      <button class="btn no" id="no-btn">Нет</button>
      <button class="btn" id="yes-btn">Да</button>
    </div>
  `;
}
function wireGate() {
  document.getElementById("yes-btn").addEventListener("click", () => {
    state.wizard = freshWizard();
    enterWizardScreen();
  });
  document.getElementById("no-btn").addEventListener("click", () => { state.screen = "known"; render(); });
}

function renderKnown() {
  return `
    <div class="icon-xl">🤔</div>
    <div class="question-box"><div class="q-text">Уже знаете, куда поедете?</div></div>
    <div class="row">
      <button class="btn no" id="no-btn">Нет</button>
      <button class="btn" id="yes-btn">Да</button>
    </div>
    <button class="btn secondary" id="back-btn">← Назад</button>
  `;
}
function wireKnown() {
  document.getElementById("yes-btn").addEventListener("click", () => { state.screen = "farewell"; render(); });
  document.getElementById("no-btn").addEventListener("click", () => { state.screen = "travelers"; render(); });
  document.getElementById("back-btn").addEventListener("click", () => { state.screen = "gate"; render(); });
}

function renderFarewell() {
  return `
    <div class="icon-xl">👋</div>
    <h1 class="title">Хорошего пути!</h1>
    <p class="subtitle">Возвращайтесь, если понадобится помощь с выбором следующего направления.</p>
    <button class="btn" id="menu-btn">Закрыть</button>
    <button class="btn secondary" id="back-btn">← Назад</button>
  `;
}
function wireFarewell() {
  document.getElementById("menu-btn").addEventListener("click", () => { state.screen = "menu"; render(); });
  document.getElementById("back-btn").addEventListener("click", () => { state.screen = "known"; render(); });
}

// Ветка "да/нет": человек не знает направление, но и подбор не хочет - явное противоречие.
// Вместо того чтобы просто попрощаться, показываем реальные направления из базы знаний
// (как "примеры результатов" на схеме) и предлагаем всё-таки попробовать подбор.
function renderTravelers() {
  const pool = loadDestinations().slice();
  const sample = [];
  for (let i = 0; i < 3 && pool.length; i++) {
    sample.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
  }
  state.travelersSample = sample;
  const cards = sample.map((d, i) => `
    <div class="card">
      <div class="icon-xl" id="traveler-icon-${i}" style="font-size:40px;margin-bottom:6px;">🧳</div>
      <b>${escapeHtml(d.country)} — ${escapeHtml(d.city)}</b><br>${escapeHtml(d.description)}
    </div>
  `).join("");
  return `
    <h1 class="title">Посмотрите, куда уже поехали другие</h1>
    <p class="subtitle">Реальные направления из базы знаний — возможно, одно из них приглянется и вам.</p>
    ${cards}
    <button class="btn" id="try-btn">Всё-таки попробовать подбор</button>
    <button class="btn no" id="menu-btn">Закрыть</button>
    <button class="btn secondary" id="back-btn">← Назад</button>
  `;
}
function wireTravelers() {
  document.getElementById("try-btn").addEventListener("click", () => {
    state.wizard = freshWizard();
    enterWizardScreen();
  });
  document.getElementById("menu-btn").addEventListener("click", () => { state.screen = "menu"; render(); });
  document.getElementById("back-btn").addEventListener("click", () => { state.screen = "known"; render(); });

  const sample = state.travelersSample || [];
  sample.forEach((d, i) => {
    fetchDestinationImageUrl(d.country, d.city).then((url) => {
      if (!url || state.travelersSample !== sample) return;
      const holder = document.getElementById(`traveler-icon-${i}`);
      if (!holder) return;
      const img = new Image();
      img.onload = () => { holder.innerHTML = ""; holder.appendChild(img); };
      img.src = url;
      img.alt = `${d.country} — ${d.city}`;
      img.style.maxHeight = "160px";
    });
  });
}

// ---------------- Мастер вопросов ----------------
// Пропускает категории, где пользователь ответил "нет" на все варианты (критерий не важен),
// пока не найдёт категорию с реальным вопросом, либо пока не дойдёт до конца мастера.
function skipExhaustedCategories() {
  const w = state.wizard;
  while (w.catIndex < CATEGORIES.length) {
    const options = DATA[CATEGORIES[w.catIndex].table];
    if (w.optIndex < options.length) return;
    w.answers[CATEGORIES[w.catIndex].code] = "any";
    w.catIndex += 1;
    w.optIndex = 0;
  }
}

function enterWizardScreen() {
  skipExhaustedCategories();
  if (state.wizard.catIndex >= CATEGORIES.length) { finish(); return; }
  state.screen = "wizard";
  render();
}

function renderWizard() {
  const w = state.wizard;
  const cat = CATEGORIES[w.catIndex];
  const options = DATA[cat.table];
  const opt = options[w.optIndex];
  const icon = opt[opt.length - 1];
  const total = CATEGORIES.length;

  let question;
  if (cat.code === "budget") {
    const [, name, min, max] = opt;
    const price = min === 0 ? `до ${max}$` : max >= 999999 ? `более ${min}$` : `${min}$ – ${max}$`;
    question = `Подходит бюджет «${name}» (${price})?`;
  } else {
    question = `Вам подходит вариант «${opt[1]}»?`;
  }

  return `
    <p class="subtitle">Шаг ${w.catIndex + 1} из ${total}: ${cat.title}</p>
    <div class="progress-wrap"><div class="progress-bar" style="width:${(w.catIndex / total) * 100}%"></div></div>
    <div class="icon-xl">${icon}</div>
    <div class="question-box"><div class="q-text">${escapeHtml(question)}</div></div>
    <div class="row">
      <button class="btn no" id="no-btn">Нет</button>
      <button class="btn" id="yes-btn">Да</button>
    </div>
    <button class="btn secondary" id="back-btn" ${w.historyStack.length ? "" : "disabled"}>← Назад</button>
  `;
}

function wireWizard() {
  const w = state.wizard;
  document.getElementById("yes-btn").addEventListener("click", () => {
    w.historyStack.push({ catIndex: w.catIndex, optIndex: w.optIndex, answers: { ...w.answers } });
    const cat = CATEGORIES[w.catIndex];
    w.answers[cat.code] = DATA[cat.table][w.optIndex][0];
    w.catIndex += 1;
    w.optIndex = 0;
    enterWizardScreen();
  });
  document.getElementById("no-btn").addEventListener("click", () => {
    w.historyStack.push({ catIndex: w.catIndex, optIndex: w.optIndex, answers: { ...w.answers } });
    w.optIndex += 1;
    enterWizardScreen();
  });
  if (w.historyStack.length) {
    document.getElementById("back-btn").addEventListener("click", () => {
      const snap = w.historyStack.pop();
      w.catIndex = snap.catIndex; w.optIndex = snap.optIndex; w.answers = snap.answers;
      state.screen = "wizard";
      render();
    });
  }
}

function finish() {
  const w = state.wizard;
  const query = {
    budget: w.answers.budget ?? "any", continent: w.answers.continent ?? "any",
    type: w.answers.type ?? "any", season: w.answers.season ?? "any",
    duration: w.answers.duration ?? "any", visa: w.answers.visa ?? "any",
    interest: w.answers.interest ?? "any",
  };

  state.query = query;
  state.filters = emptyFilters();
  state.historyId = null;
  if (state.wizardUseAi) think("ИИ сравнивает направления", () => acceptResult(runAi(state.query, state.filters)));
  else finishByRules();
}

function finishByRules() {
  const result = recommendRule(loadDestinations(), state.query);
  result.criteria = compareWithQuery(result.destination, state.query, NAMES);
  result.cost = estimateCost(result.destination, state.query, getSettings().travelers);
  acceptResult(result);
}

function runAi(query, filters) {
  // Последние подборы из истории: среди равноценных вариантов ИИ предложит новые.
  const recent = new Set(getHistory().slice(0, 15).map((h) => `${h.country}|${h.city}`));
  return recommendSmart(loadDestinations(), query, getSettings().travelers, NAMES, recent, filters);
}

/** Общий финал: шутка про невозможное сочетание, история, экран результата. */
function acceptResult(result) {
  const q = state.query;
  applyAbsurdNote(result, q.continent, q.type, q.duration);
  const summary = { country: result.country, city: result.city, costUsd: result.cost.total };
  if (state.historyId) {
    updateHistoryEntry(state.historyId, summary);
  } else {
    state.historyId = `h-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    addHistoryEntry({ id: state.historyId, dt: new Date().toLocaleString("ru-RU"), ...q, useAi: state.wizardUseAi, ...summary });
  }
  state.lastResult = result;
  go("result");
}

/** Уточнение обычным текстом: ИИ разбирает его и подбирает заново. */
async function refine(wish) {
  wish = wish.trim();
  const current = state.lastResult;
  if (!wish || current.source !== "ai") return;
  const rate = await ensureRate();
  if (state.screen !== "result" || state.lastResult !== current) return; // пользователь уже ушёл с экрана
  const message = (text) => { document.getElementById("refine-note").textContent = text; };
  const countries = new Set(loadDestinations().map((d) => d.country));
  const { query, filters, notes } = parseWish(wish, state.query, state.filters, current, countries, rate?.rate, NAMES);
  if (!notes.length) {
    message("Не понял уточнение. Попробуйте, например: «не больше 1000 $», «без Испании», «с детьми на море», «на 5 дней зимой».");
    return;
  }
  if (JSON.stringify([query, filters]) === JSON.stringify([state.query, state.filters])) {
    message(`Это уже учтено: ${notes.join("; ")}.`);
    return;
  }
  const result = runAi(query, filters);
  if (!result) {
    message(`Понял: ${notes.join("; ")}. Но таких направлений в базе нет, поэтому оставил прежний вариант.`);
    return;
  }
  result.refinements = current.refinements.concat(notes);
  think("ИИ учитывает уточнение", () => {
    state.query = query;
    state.filters = filters;
    acceptResult(result);
  });
}

/** Короткий экран «ИИ думает», после которого выполняется then(). */
function think(text, then) {
  state.thinkingText = text;
  go("thinking");
  setTimeout(() => { if (state.screen === "thinking") then(); }, 1000);
}

// ---------------- "ИИ думает" ----------------
let dotTimer = null;
function renderThinking() {
  return `
    <div style="flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;">
      <div class="icon-xl">🤖</div>
      <p id="thinking-text" style="font-size:17px;font-weight:600;margin:10px 0 18px;">${state.thinkingText}</p>
      <div class="progress-wrap" style="width:220px;"><div class="progress-bar indeterminate"></div></div>
    </div>
  `;
}
function wireThinking() {
  let dots = 0;
  clearInterval(dotTimer);
  dotTimer = setInterval(() => {
    dots = (dots + 1) % 4;
    const el = document.getElementById("thinking-text");
    if (el) el.textContent = state.thinkingText + ".".repeat(dots);
    else clearInterval(dotTimer);
  }, 350);
}

// ---------------- Результат ----------------
const REFINE_CHIPS = [
  ["Дешевле", "Найди вариант дешевле"], ["Другая страна", "Предложи другую страну"],
  ["Спокойнее", "Хочу спокойное место"], ["Активнее", "Хочу активный отдых"],
];

function renderResult() {
  const r = state.lastResult;
  return `
    <p class="mode-label">${escapeHtml(r.mode)}</p>
    <div class="icon-xl" id="result-icon">${r.source === "rules" ? "🧭" : "🤖"}</div>
    <h1 class="title">${escapeHtml(r.country)} — ${escapeHtml(r.city)}</h1>
    <p class="subtitle">${escapeHtml(r.description)}</p>
    ${r.refinements?.length ? `<p class="subtitle">Учтено: ${escapeHtml(r.refinements.join("; "))}</p>` : ""}
    <div class="reasoning-box" id="reasoning-box">${escapeHtml(r.reasoning)}</div>
    ${costCardHtml(r.cost)}
    ${r.source === "ai" ? renderAiDetails(r) : ""}
    <button class="btn secondary" id="explain-btn">Как получен этот результат?</button>
    <button class="btn secondary" id="discuss-btn">💬 Обсудить с ИИ-консультантом</button>
    <button class="btn" id="again-btn">Пройти ещё раз</button>
    <button class="btn no" id="menu-btn">В меню</button>
  `;
}

function renderAiDetails(r) {
  const block = (title, lines) => (lines.length
    ? `<div class="card"><b>${title}</b>${lines.map((l) => `<div>${escapeHtml(l)}</div>`).join("")}</div>` : "");
  const [matched, asked] = countMatches(r.criteria);
  return `
    ${block("💡 Советы", r.tips.map((t) => `• ${t}`))}
    ${block("🔁 Запасные варианты", r.alternatives.map((a) =>
    `• ${a.country} — ${a.city} (${a.percent}%, ≈ ${formatUsd(a.total)}): ${a.reason}`))}
    <p class="confidence-label">Соответствие запросу: ${r.confidence}%${asked ? ` · совпало критериев: ${matched} из ${asked}` : ""}</p>
    <div class="progress-wrap"><div class="progress-bar" id="conf-bar" style="width:0%"></div></div>
    <form class="refine" id="refine-form">
      <label for="refine-text"><b>✏️ Уточнить ответ ИИ</b></label>
      <div class="chat-input"><input id="refine-text" maxlength="500" placeholder="С детьми на море, до 1000 $"><button class="btn">Уточнить</button></div>
      ${REFINE_CHIPS.map(([t], i) => `<button type="button" class="chip inline" data-refine="${i}">${t}</button>`).join("")}
      <p class="status-note bad" id="refine-note"></p>
    </form>`;
}

function wireResult() {
  const r = state.lastResult;
  document.getElementById("explain-btn").addEventListener("click", () => go("explanation"));
  document.getElementById("discuss-btn").addEventListener("click", () => { state.chatBack = "result"; go("consultant"); });
  document.getElementById("again-btn").addEventListener("click", () => { state.wizard = freshWizard(); enterWizardScreen(); });
  document.getElementById("menu-btn").addEventListener("click", () => go("menu"));
  wireCostCard(r.cost);

  if (r.source === "ai") {
    document.getElementById("refine-form").addEventListener("submit", (e) => {
      e.preventDefault();
      refine(document.getElementById("refine-text").value);
    });
    root.querySelectorAll("[data-refine]").forEach((b) =>
      b.addEventListener("click", () => refine(REFINE_CHIPS[Number(b.dataset.refine)][1])));
    const bar = document.getElementById("conf-bar");
    if (bar) setTimeout(() => { bar.style.width = r.confidence + "%"; }, 50);
  }
  // Эффект печати - только при первом показе, а не при каждом возврате на экран.
  if (r.source !== "rules" && state.resultAnimated !== r) {
    typewriter(document.getElementById("reasoning-box"), r.reasoning);
  }
  state.resultAnimated = r;

  fetchDestinationImageUrl(r.country, r.city).then((url) => {
    if (!url || state.lastResult !== r) return;
    const holder = document.getElementById("result-icon");
    if (!holder) return;
    const img = new Image();
    img.onload = () => { holder.innerHTML = ""; holder.appendChild(img); };
    img.src = url;
    img.alt = `${r.country} — ${r.city}`;
  });
}

let twTimer = null;
function typewriter(el, text, step = 2, interval = 14) {
  clearInterval(twTimer);
  let i = 0;
  twTimer = setInterval(() => {
    i += step;
    el.textContent = text.slice(0, i);
    if (i >= text.length) clearInterval(twTimer);
  }, interval);
}

// ---------------- Объяснение ----------------
const STATUS_MARKS = { match: ["✅", "ok"], partial: ["🟡", "mid"], mismatch: ["❌", "bad"] };

function renderExplanation() {
  const r = state.lastResult;
  const isAi = r.source === "ai";
  let n = 0;
  const heading = (t) => `<h3 class="step-title">${++n}. ${t}</h3>`;
  const line = (t, cls = "plain") => `<div class="trace-step ${cls}">${escapeHtml(t)}</div>`;
  const note = (t) => `<p class="meta-line">${escapeHtml(t)}</p>`;
  const asked = r.criteria.filter((c) => c.status !== "any");
  const free = r.criteria.filter((c) => c.status === "any").map((c) => c.title.toLowerCase());
  const out = [];

  out.push(heading("Ваши ответы"));
  if (asked.length) out.push(line(asked.map((c) => `${c.title}: ${c.wanted}`).join("\n")));
  if (free.length) out.push(note("Не важно (в подборе не участвует): " + free.join(", ")));
  (r.refinements ?? []).forEach((w) => out.push(line(`✏️ Уточнение: ${w}`, "mid")));

  if (isAi) {
    out.push(heading("Сравнение направлений"));
    out.push(note("ИИ не отбрасывает условия по очереди, а оценивает каждое направление по всем важным для вас критериям сразу: чем важнее критерий и точнее совпадение, тем больше баллов."));
    const skipped = r.totalDestinations - r.considered;
    out.push(line(`Сравнил направлений: ${r.considered}${skipped ? ` (ещё ${skipped} исключено вашими уточнениями)` : ""}. Соответствие 80% и выше: ${r.strong}.`));
    r.ranking.forEach((item, i) => out.push(line(`${item.chosen ? "✅ " : ""}${i + 1}. ${item.place}: ${item.percent}%`, item.chosen ? "ok" : "no")));
    if (r.equal > 1) {
      out.push(note(`Почти равных лидеров: ${r.equal}, из них выбран один случайно${r.skippedRecent ? `; недавно показанные вам (${r.skippedRecent}) пропущены` : ""}.`));
    }
  } else {
    out.push(heading("Отбор по базе знаний"));
    out.push(note("Условия ослабляются от наименее важного к самому важному, пока не найдётся совпадение."));
    r.trace.forEach((s) => out.push(line(`${s.success ? "✅" : "❌"} ${s.stage} — совпадений: ${s.count}`, s.success ? "ok" : "no")));
    const { count } = r.trace[r.trace.length - 1];
    out.push(heading("Выбор варианта"));
    out.push(line(count > 1
      ? `Подошло направлений: ${count}. Для правил они равноценны, поэтому одно выбрано случайно.`
      : "Подошло единственное направление, оно и предложено."));
  }

  out.push(heading("Сверка по критериям"));
  if (!asked.length) {
    out.push(note("Вы не задали ни одного критерия, поэтому подходит любое направление."));
  } else {
    asked.forEach((c) => {
      const [mark, cls] = STATUS_MARKS[c.status];
      const points = isAi ? `\n${(c.weight * c.degree).toFixed(1)} из ${c.weight} баллов` : "";
      out.push(line(`${mark} ${c.title}: вы хотели «${c.wanted}», у направления «${c.actual}»${points}`, cls === "ok" ? "plain" : cls));
    });
    if (isAi) {
      const earned = asked.reduce((s, c) => s + c.weight * c.degree, 0), total = asked.reduce((s, c) => s + c.weight, 0);
      out.push(line(`Итого ${earned.toFixed(1)} из ${total} баллов = соответствие ${r.confidence}%`, "ok"));
      out.push(note("«Любой» у направления даёт чуть меньше полного балла: в базе нет ограничения, но нет и подтверждения, что место именно для этого."));
    } else {
      const [matched, total] = countMatches(r.criteria);
      out.push(note(`Совпало полностью или частично: ${matched} из ${total}`));
    }
  }

  out.push(heading("Как посчитана сумма"));
  r.cost.items.forEach((i) => out.push(line(`${i.category}: ${formatUsd(i.amount)}`, "no")));
  out.push(`<div class="trace-step ok">Итого на ${r.cost.travelers} чел.: ≈ ${formatUsd(r.cost.total)}<span id="explain-kzt"></span></div>`);
  out.push(note("Основа - уровень цен направления в базе, сдвинутый по длительности поездки и умноженный на число путешественников. По статьям сумма разделена типичными долями: дорога зависит от дальности континента, остальное - от типа отдыха. Это ориентир, а не цены на билеты."));
  out.push(`<p class="meta-line" id="explain-rate"></p>`);

  return `
    <div class="top-bar"><h2>Ход рассуждения</h2></div>
    <p class="subtitle">Итоговый выбор: ${escapeHtml(r.country)} — ${escapeHtml(r.city)}</p>
    <p class="mode-label">${escapeHtml(r.mode)}</p>
    ${out.join("")}
    <button class="btn" id="close-explain">Закрыть</button>
  `;
}
function wireExplanation() {
  const { cost } = state.lastResult;
  const kzt = document.getElementById("explain-kzt"), rateEl = document.getElementById("explain-rate");
  ensureRate().then((rate) => {
    if (rate) kzt.textContent = ` (${formatKzt(cost.total, rate)})`;
    rateEl.textContent = describeRate(rate);
  });
  document.getElementById("close-explain").addEventListener("click", () => go("result"));
}

// ---------------- История ----------------
function renderHistory() {
  const rows = getHistory();
  const body = rows.length
    ? rows.map((h) => `
      <div class="card">
        <div class="meta">${escapeHtml(h.dt)} · ${h.useAi ? "ИИ" : "Правила"}</div>
        <b>${escapeHtml(h.country)} — ${escapeHtml(h.city)}</b>${Number.isFinite(h.costUsd) ? ` · ≈ ${formatUsd(h.costUsd)}` : ""}<br>
        <span class="meta">${NAMES.budget[h.budget]} · ${NAMES.continent[h.continent]} · ${NAMES.type[h.type]} · ${NAMES.season[h.season]} · ${NAMES.duration[h.duration]} · ${NAMES.visa[h.visa]} · ${NAMES.interest[h.interest]}</span>
      </div>
    `).join("")
    : `<p class="empty-note">Пока пусто — здесь появится история ваших подборов.</p>`;
  return `
    <div class="top-bar"><h2>История подборов</h2></div>
    ${body}
    <button class="btn no" id="clear-history-btn" ${rows.length ? "" : "disabled"}>Очистить историю</button>
    <button class="btn secondary" id="back-menu-btn">Назад</button>
  `;
}
function wireHistory() {
  const clearBtn = document.getElementById("clear-history-btn");
  if (!clearBtn.disabled) {
    clearBtn.addEventListener("click", () => {
      if (confirm("Очистить всю историю подборов?")) { clearHistory(); render(); }
    });
  }
  document.getElementById("back-menu-btn").addEventListener("click", () => { state.screen = "menu"; render(); });
}

// ---------------- База знаний ----------------
function optionSelect(id, table, withAny, anyLabel) {
  const opts = DATA[table].map((r) => `<option value="${r[0]}">${escapeHtml(r[1])}</option>`).join("");
  const anyOpt = withAny ? `<option value="any" selected>${anyLabel}</option>` : "";
  return `<select id="${id}">${anyOpt}${opts}</select>`;
}

function renderKb() {
  const all = loadDestinations().filter((d) => {
    if (!state.kbFilter) return true;
    const f = state.kbFilter.toLowerCase();
    return d.country.toLowerCase().includes(f) || d.city.toLowerCase().includes(f);
  });

  const list = all.length
    ? all.map((d) => `
      <div class="card">
        <b>${escapeHtml(d.country)} — ${escapeHtml(d.city)}</b>
        <button class="btn no small" data-del="${d.id}" style="float:right;">Удалить</button>
        <div class="meta">${NAMES.budget[d.budget]} · ${NAMES.continent[d.continent]} · ${NAMES.type[d.type]} · ${d.season === "any" ? "любой сезон" : NAMES.season[d.season]}</div>
        <div>${escapeHtml(d.description)}</div>
      </div>
    `).join("")
    : `<p class="empty-note">Ничего не найдено.</p>`;

  return `
    <div class="top-bar"><h2>База знаний</h2></div>
    <p class="subtitle">Каждая запись — правило: при таком сочетании критериев система предложит именно это направление.</p>

    <div class="field"><label>Бюджет</label>${optionSelect("f-budget", "BUDGETS")}</div>
    <div class="field"><label>Континент</label>${optionSelect("f-continent", "CONTINENTS")}</div>
    <div class="field"><label>Тип отдыха</label>${optionSelect("f-type", "REST_TYPES")}</div>
    <div class="field"><label>Сезон</label>${optionSelect("f-season", "SEASONS", true, "Любой сезон")}</div>
    <div class="field"><label>Длительность</label>${optionSelect("f-duration", "DURATIONS", true, "Любая длительность")}</div>
    <div class="field"><label>Виза</label>${optionSelect("f-visa", "VISAS", true, "Любой визовый режим")}</div>
    <div class="field"><label>Интерес</label>${optionSelect("f-interest", "INTERESTS", true, "Любой интерес")}</div>
    <div class="field"><label>Страна</label><input id="f-country" placeholder="Страна"></div>
    <div class="field"><label>Город</label><input id="f-city" placeholder="Город"></div>
    <div class="field"><label>Описание</label><input id="f-description" placeholder="Краткое описание направления"></div>
    <button class="btn" id="add-dest-btn">Добавить направление</button>

    <div class="field" style="margin-top:18px;"><label>Поиск по списку</label><input id="kb-filter" placeholder="Страна или город" value="${escapeHtml(state.kbFilter)}"></div>
    ${list}
    <button class="btn secondary" id="back-menu-btn">Назад</button>
  `;
}

function wireKb() {
  document.getElementById("add-dest-btn").addEventListener("click", () => {
    const country = document.getElementById("f-country").value.trim();
    const city = document.getElementById("f-city").value.trim();
    const description = document.getElementById("f-description").value.trim() || "Описание не указано.";
    if (!country || !city) { alert("Укажите страну и город!"); return; }

    addCustomDestination({
      id: makeDestinationId(),
      budget: document.getElementById("f-budget").value,
      continent: document.getElementById("f-continent").value,
      type: document.getElementById("f-type").value,
      season: document.getElementById("f-season").value,
      duration: document.getElementById("f-duration").value,
      visa: document.getElementById("f-visa").value,
      interest: document.getElementById("f-interest").value,
      country, city, description, builtin: false,
    });
    render();
  });

  const filterInput = document.getElementById("kb-filter");
  filterInput.addEventListener("input", (e) => {
    state.kbFilter = e.target.value;
    render();
    const el = document.getElementById("kb-filter");
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  });

  root.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-del");
      if (!confirm("Удалить это направление из базы знаний?")) return;
      if (id.startsWith("builtin-")) deleteBuiltinDestination(id);
      else removeCustomDestination(id);
      render();
    });
  });

  document.getElementById("back-menu-btn").addEventListener("click", () => { state.screen = "menu"; render(); });
}

render();
