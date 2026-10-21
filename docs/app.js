import DATA from "./data.js";
import { recommendRule, recommendAI, applyAbsurdNote } from "./engine.js";
import { fetchDestinationImageUrl } from "./wiki.js";
import { loadDestinations, makeDestinationId } from "./destinations.js";
import {
  getHistory, addHistoryEntry, clearHistory,
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
};

function freshWizard() {
  return { catIndex: 0, optIndex: 0, answers: {}, historyStack: [] };
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const SCREEN_RENDERERS = {
  menu: renderMenu, gate: renderGate, known: renderKnown, farewell: renderFarewell, travelers: renderTravelers,
  wizard: renderWizard, thinking: renderThinking, result: renderResult, explanation: renderExplanation,
  history: renderHistory, kb: renderKb,
};
const SCREEN_WIRERS = {
  menu: wireMenu, gate: wireGate, known: wireKnown, farewell: wireFarewell, travelers: wireTravelers,
  wizard: wireWizard, thinking: wireThinking, result: wireResult, explanation: wireExplanation,
  history: wireHistory, kb: wireKb,
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
    <p class="subtitle">${state.useAi ? "Режим ИИ включён — ответы формируются в разговорном стиле с оценкой уверенности" : "В режиме ИИ рекомендация формируется в разговорном стиле с оценкой уверенности"}</p>
    <button class="btn" id="start-btn">Начать подбор путешествия</button>
    <button class="btn secondary" id="history-btn">История подборов</button>
    <button class="btn secondary" id="kb-btn">База знаний (направления)</button>
    <footer class="hint">Работает полностью в браузере — история и добавленные направления хранятся только на этом устройстве.</footer>
  `;
}

function wireMenu() {
  document.getElementById("ai-toggle").addEventListener("change", (e) => {
    state.useAi = e.target.checked;
    render();
  });
  document.getElementById("start-btn").addEventListener("click", () => { state.screen = "gate"; render(); });
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

  const compute = (engine) => {
    const destinations = loadDestinations();
    const result = engine(destinations, query);
    applyAbsurdNote(result, query.continent, query.type, query.duration);
    addHistoryEntry({ dt: new Date().toLocaleString("ru-RU"), ...query, useAi: state.useAi, country: result.country, city: result.city });
    state.lastResult = result;
    state.screen = "result";
    render();
  };

  if (state.useAi) {
    state.screen = "thinking";
    render();
    setTimeout(() => compute(recommendAI), 1400);
  } else {
    compute(recommendRule);
  }
}

// ---------------- "ИИ думает" ----------------
let dotTimer = null;
function renderThinking() {
  return `
    <div style="flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;">
      <div class="icon-xl">🤖</div>
      <p id="thinking-text" style="font-size:17px;font-weight:600;margin:10px 0 18px;">ИИ анализирует ваши ответы</p>
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
    if (el) el.textContent = "ИИ анализирует ваши ответы" + ".".repeat(dots);
    else clearInterval(dotTimer);
  }, 350);
}

// ---------------- Результат ----------------
function renderResult() {
  const r = state.lastResult;
  return `
    <p class="mode-label">${r.mode}</p>
    <div class="icon-xl" id="result-icon">${state.useAi ? "🤖" : "🧭"}</div>
    <h1 class="title">${escapeHtml(r.country)} — ${escapeHtml(r.city)}</h1>
    <p class="subtitle">${escapeHtml(r.description)}</p>
    <div class="reasoning-box" id="reasoning-box">${state.useAi ? "" : escapeHtml(r.reasoning)}</div>
    ${state.useAi ? `
      <p class="confidence-label">Уверенность ИИ: ${r.confidence}%</p>
      <div class="progress-wrap"><div class="progress-bar" id="conf-bar" style="width:0%"></div></div>
    ` : ""}
    <button class="btn secondary" id="explain-btn">Как получен этот результат?</button>
    <button class="btn" id="again-btn">Пройти ещё раз</button>
    <button class="btn no" id="menu-btn">В меню</button>
  `;
}

function wireResult() {
  const r = state.lastResult;
  document.getElementById("explain-btn").addEventListener("click", () => { state.screen = "explanation"; render(); });
  document.getElementById("again-btn").addEventListener("click", () => { state.wizard = freshWizard(); enterWizardScreen(); });
  document.getElementById("menu-btn").addEventListener("click", () => { state.screen = "menu"; render(); });

  if (state.useAi) {
    typewriter(document.getElementById("reasoning-box"), r.reasoning);
    const bar = document.getElementById("conf-bar");
    if (bar) setTimeout(() => { bar.style.width = r.confidence + "%"; }, 50);
  }

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
function renderExplanation() {
  const r = state.lastResult;
  const steps = r.trace.map((s) => `
    <div class="trace-step ${s.success ? "ok" : "no"}">${s.success ? "✅" : "❌"} ${escapeHtml(s.stage)} — совпадений: ${s.count}</div>
  `).join("");
  const confNote = state.useAi ? `<div class="reasoning-box" style="margin-top:10px;">🤖 Уверенность ИИ (${r.confidence}%) считается по правилу: чем меньше критериев пришлось ослабить выше, тем выше итоговая уверенность.</div>` : "";
  return `
    <div class="top-bar"><h2>Ход рассуждения</h2></div>
    <p class="subtitle">Итоговый выбор: ${escapeHtml(r.country)} — ${escapeHtml(r.city)}</p>
    ${steps}
    ${confNote}
    <button class="btn" id="close-explain">Закрыть</button>
  `;
}
function wireExplanation() {
  document.getElementById("close-explain").addEventListener("click", () => { state.screen = "result"; render(); });
}

// ---------------- История ----------------
function renderHistory() {
  const rows = getHistory();
  const body = rows.length
    ? rows.map((h) => `
      <div class="card">
        <div class="meta">${escapeHtml(h.dt)} · ${h.useAi ? "ИИ" : "Правила"}</div>
        <b>${escapeHtml(h.country)} — ${escapeHtml(h.city)}</b><br>
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
