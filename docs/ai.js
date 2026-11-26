// Порт MyProgram/ai_service.py: ИИ-консультант (настройки, клиент OpenAI-совместимого API, промпт) и
// курс тенге. Подбор внешний ИИ не использует - он в smart.js. Экраны лежат в ai-ui.js.
//
// Ключ в коде хранить нельзя: сайт статический, его исходники видны всем. Либо ключ лежит на прокси
// (PROXY_URL), либо вводится в настройках - тогда он живёт до закрытия вкладки
// (sessionStorage), а в localStorage попадает лишь с явного согласия.

const SETTINGS_KEY = "travel_expert_ai_settings_v1";
const API_KEY = "travel_expert_ai_key_v1";
const RATE_KEY = "travel_expert_usd_kzt_v1";
const CHAT_KEY = "travel_expert_chat_v1";

// Режимы ИИ. Все сервисы понимают один формат запросов, отличаются адрес, модель и ключ. Ключ каждого
// режима хранится отдельно и уходит только на адрес своего режима.
export const PROVIDERS = {
  1: { baseUrl: "https://api.openai.com/v1", model: "gpt-6-luna", fallbacks: [] },
  2: {
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-3.5-flash-lite",
    fallbacks: ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"],
  },
  3: { baseUrl: "https://api.groq.com/openai/v1", model: "openai/gpt-oss-120b", fallbacks: ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"] },
};
// Перегрузка сервиса (503 и похожее) обычно проходит за секунды, поэтому запрос повторяется сам:
// та же модель, затем запасные по очереди. У запасных свои лимиты, поэтому они выручают и при 429.
const RETRY_STATUSES = new Set([500, 502, 503, 504]);
const RETRY_DELAY = 2000, FALLBACK_DELAY = 1000;
// Модель, ответившая последней, - с неё начинается следующий запрос (до перезагрузки страницы).
const preferredModels = {};

// Режим, с которым сайт открывается, пока в настройках не выбран другой (как в MyProgram/ai_service.py).
const DEFAULT_PROVIDER = 2;

// Адрес своего прокси (см. proxy/). Если указан, ключ посетителям не нужен, а режим выбирается на прокси.
const PROXY_URL = "";

const DEFAULTS = {
  provider: DEFAULT_PROVIDER,
  baseUrl: "", // свой сервер вместо адреса режима
  departureCity: "Алматы", citizenship: "Казахстан", travelers: 1,
};

// ---------------- Хранилище ----------------
function readJSON(storage, key, fallback) {
  try {
    const raw = window[storage].getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}
function writeItem(storage, key, value) {
  try {
    if (value === null) window[storage].removeItem(key);
    else window[storage].setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

let memoryKeys = {}; // запасной вариант, если хранилища браузера недоступны

export function getSettings() {
  const stored = readJSON("localStorage", SETTINGS_KEY, {}) || {};
  const settings = { ...DEFAULTS };
  for (const k of Object.keys(DEFAULTS)) {
    if (typeof stored[k] === typeof DEFAULTS[k]) settings[k] = stored[k];
  }
  settings.travelers = Math.min(20, Math.max(1, Math.round(settings.travelers) || 1));
  if (!PROVIDERS[settings.provider]) settings.provider = DEFAULT_PROVIDER;
  const remembered = readJSON("localStorage", API_KEY, null);
  const keys = remembered || readJSON("sessionStorage", API_KEY, null) || memoryKeys;
  settings.remember = Boolean(remembered);
  settings.keys = {};
  for (const n of Object.keys(PROVIDERS)) settings.keys[n] = typeof keys[n] === "string" ? keys[n] : "";
  return settings;
}

export function saveSettings({ keys, remember, ...rest }) {
  const hasKeys = Object.values(keys).some(Boolean);
  writeItem("localStorage", SETTINGS_KEY, rest);
  memoryKeys = keys;
  writeItem("localStorage", API_KEY, remember && hasKeys ? keys : null);
  writeItem("sessionStorage", API_KEY, !remember && hasKeys ? keys : null);
}

const effectiveKey = (s) => s.keys[s.provider];
const effectiveBaseUrl = (s) => (s.baseUrl || PROXY_URL || PROVIDERS[s.provider].baseUrl).replace(/\/+$/, "");
const effectiveModel = (s) => PROVIDERS[s.provider].model;
/** Прокси и свои серверы работают без ключа: он хранится на их стороне. */
const needsKey = (s) => !(s.baseUrl || PROXY_URL);
export function isConfigured() {
  const s = getSettings();
  return Boolean(effectiveKey(s)) || !needsKey(s);
}

export function validateBaseUrl(text) {
  let url;
  try { url = new URL(text); } catch { return "Укажите полный адрес сервера, начиная с https://"; }
  if (url.protocol === "https:") return null;
  if (url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) return null;
  return "Адрес сервера должен начинаться с https:// - иначе ключ уйдёт по сети незашифрованным.";
}

export function getChat() {
  const list = readJSON("localStorage", CHAT_KEY, []);
  return Array.isArray(list)
    ? list.filter((m) => m && ["user", "assistant"].includes(m.role) && typeof m.content === "string")
    : [];
}
export function saveChat(list) { writeItem("localStorage", CHAT_KEY, list); }

// ---------------- Клиент ----------------
// 429 бывает двух видов: временное "слишком часто" и "нет денег/исчерпан лимит расходов" - второе
// повтором не лечится. Точная причина в error.code, но error.type тоже может быть insufficient_quota.
const BILLING = /quota|credit|billing|spend_limit|usage_limit/;

/** Режим 2 сообщает о неверном ключе кодом 400, а об отозванном - 403. */
const isKeyError = (status, detail) => [400, 403].includes(status) && /api key|authorization header/i.test(detail);

/** [код, текст] ошибки из ответа API. */
function parseError(bodyText) {
  try {
    let data = JSON.parse(bodyText);
    if (Array.isArray(data)) [data] = data; // режим 2 заворачивает ошибку в список
    const err = data.error;
    if (err && typeof err === "object") return [[err.code, err.type, err.status].filter(Boolean).join(" "), String(err.message || "")];
    return ["", String(err || "")];
  } catch {
    return ["", ""];
  }
}

const isBillingError = (status, bodyText) => [402, 429].includes(status) && BILLING.test(parseError(bodyText)[0]);

function httpError(status, bodyText) {
  const [code, detail] = parseError(bodyText);

  console.warn(`[ai] HTTP ${status} ${code}: ${detail.slice(0, 300)}`); // технический текст - не для экрана

  if (status === 401 || isKeyError(status, detail)) return "Ключ доступа не принят.";
  if (status === 429 && /per.?day/i.test(bodyText)) return "Дневной лимит ИИ исчерпан. Попробуйте завтра.";
  if (isBillingError(status, bodyText)) return "На балансе сервиса ИИ нет средств или исчерпан лимит расходов. Пополните баланс ключа доступа.";
  if (status === 429) return "Слишком много запросов или исчерпан лимит. Подождите немного и повторите.";
  let base = `Сервис ИИ отклонил запрос (код ${status}).`;
  if (/model_not_found|NOT_FOUND/.test(code)) base = "Модель ИИ недоступна для этого ключа доступа.";
  else if (status === 404) base = "Сервер ИИ не найден.";
  else if (status === 403) base = "Сервис ИИ запретил доступ.";
  else if (status >= 500) base = "Сервис ИИ сейчас перегружен. Попробуйте через пару минут.";
  return base;
}

const TIMEOUT_MS = 120000;

/** Сетевой запрос с таймаутом бездействия. Отмена через signal пробрасывается как AbortError. */
async function send(settings, path, body, signal, handle) {
  const key = effectiveKey(settings);
  if (!key && needsKey(settings)) throw new Error("ИИ не подключён.");
  if (!/^[\x21-\x7e]*$/.test(key)) throw new Error("Ключ доступа содержит недопустимые символы.");
  const base = effectiveBaseUrl(settings);
  const urlError = validateBaseUrl(base);
  if (urlError) throw new Error(urlError);

  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (signal) { if (signal.aborted) onAbort(); else signal.addEventListener("abort", onAbort); }
  let timedOut = false, timer = null;
  const arm = () => {
    clearTimeout(timer);
    timer = setTimeout(() => { timedOut = true; controller.abort(); }, TIMEOUT_MS);
  };
  arm();
  try {
    const headers = key ? { Authorization: `Bearer ${key}` } : {};
    headers["Content-Type"] = "application/json";
    const response = await fetch(base + path, {
      method: "POST", headers, body: JSON.stringify(body),
      signal: controller.signal, redirect: "error", referrerPolicy: "no-referrer", credentials: "omit",
    });
    if (!response.ok) {
      const error = new Error();
      error.body = await response.text();
      error.status = response.status;
      error.message = httpError(response.status, error.body);
      throw error;
    }
    return await handle(response, arm);
  } catch (e) {
    if (timedOut) throw new Error("ИИ слишком долго не отвечает. Проверьте интернет и повторите.");
    if (e instanceof TypeError) throw new Error("Нет связи с сервисом ИИ. Проверьте интернет и повторите.");
    throw e;
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", onAbort);
  }
}

function extractMessage(bodyText) {
  let choice, message;
  try {
    choice = JSON.parse(bodyText).choices[0];
    message = choice.message;
    if (!message) throw new Error();
  } catch {
    throw new Error("Сервис ИИ прислал непонятный ответ. Попробуйте ещё раз.");
  }
  if (message.refusal) throw new Error(`ИИ отказался отвечать: ${message.refusal}`);
  if (choice.finish_reason === "length") throw new Error("Ответ ИИ получился слишком длинным и обрезан. Попробуйте ещё раз.");
  if (!message.content) throw new Error("ИИ прислал пустой ответ. Попробуйте ещё раз.");
  return message.content;
}

/** Модели в порядке попыток. Прокси перебирает их сам, поэтому ему хватает одной. */
function modelsFor(s) {
  const main = effectiveModel(s);
  if (PROXY_URL && !s.baseUrl) return [main];
  const models = [main, ...PROVIDERS[s.provider].fallbacks];
  const preferred = preferredModels[s.provider];
  return models.includes(preferred) ? [preferred, ...models.filter((m) => m !== preferred)] : models;
}

/** Попытки { delay, model }. */
function attemptPlan(models) {
  return [{ delay: 0, model: models[0] }, { delay: RETRY_DELAY, model: models[0] },
    ...models.slice(1).map((model) => ({ delay: FALLBACK_DELAY, model }))];
}

/** Оставшиеся попытки после ошибки (plan[0] - неудавшаяся); пусто - повторять бессмысленно. */
function nextPlan(plan, e, mainModel) {
  const [{ model }, ...rest] = plan;
  console.warn(`[ai] ${model}: HTTP ${e.status}`);
  if (isKeyError(e.status, e.body) || e.status === 401 || e.status === 402 || isBillingError(e.status, e.body)) return [];
  if (RETRY_STATUSES.has(e.status)) return rest;
  // Лимит исчерпан или запасная модель недоступна: помочь может только другая модель.
  if (e.status === 429 || model !== mainModel) return rest.filter((step) => step.model !== model);
  return [];
}

/** Выполняет run(попытка) по плану, пока не получится. */
async function withRetries(settings, signal, run) {
  const models = modelsFor(settings);
  let firstError = null;
  for (let plan = attemptPlan(models); ;) {
    const step = plan[0];
    if (step.delay) await pause(step.delay, signal);
    try {
      const result = await run(step);
      preferredModels[settings.provider] = step.model;
      return result;
    } catch (e) {
      if (!e.status) throw e; // обрыв посреди ответа, непонятный ответ и отмену не повторяем
      firstError ??= e;
      plan = nextPlan(plan, e, effectiveModel(settings));
      if (!plan.length) throw firstError;
    }
  }
}

/** Пауза перед повтором; отмена прерывает её сразу. */
function pause(ms, signal) {
  return new Promise((resolve, reject) => {
    const stop = () => { clearTimeout(timer); reject(new DOMException("Отменено", "AbortError")); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", stop); resolve(); }, ms);
    if (signal?.aborted) stop();
    else signal?.addEventListener("abort", stop, { once: true });
  });
}

/** Потоковый ответ. onDelta(кусок); возвращает { truncated }. */
export function streamChat(messages, onDelta, signal) {
  const settings = getSettings();
  return withRetries(settings, signal, ({ model }) => streamOnce(settings, model, messages, onDelta, signal));
}

function streamOnce(settings, model, messages, onDelta, signal) {
  return send(settings, "/chat/completions", { model, messages, stream: true }, signal, async (response, arm) => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", raw = "", got = false, finished = false, truncated = false;

    const consume = (line) => {
      if (!line.startsWith("data:")) return;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") { finished = true; return; }
      let event;
      try { event = JSON.parse(payload); } catch { return; }
      if (event?.error) {
        console.warn("[ai]", event.error);
        throw new Error("Сервис ИИ прервал ответ. Попробуйте ещё раз.");
      }
      const choice = event?.choices?.[0];
      if (!choice) return;
      if (choice.finish_reason) { finished = true; truncated = choice.finish_reason === "length"; }
      const piece = choice.delta?.content || choice.delta?.refusal || "";
      if (piece) { got = true; onDelta(piece); }
    };

    for (;;) {
      const { done, value } = await reader.read();
      arm();
      const chunk = done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (!got) raw += chunk;
      buffer += chunk;
      const lines = buffer.split("\n");
      buffer = lines.pop();
      lines.forEach((l) => consume(l.trim()));
      if (done) break;
    }
    consume(buffer.trim());

    if (!got) onDelta(extractMessage(raw)); // сервис проигнорировал stream и ответил обычным JSON
    else if (!finished) throw new Error("Соединение оборвалось до конца ответа.");
    return { truncated };
  });
}

/** Проверка самым коротким настоящим запросом: список моделей отдаётся и при пустом балансе. */
export async function checkConnection(settings) {
  await send(settings, "/chat/completions", {
    model: effectiveModel(settings), messages: [{ role: "user", content: "Ответь одним словом: ок" }],
  }, null, (r) => r.text());
}

// ---------------- Курс тенге ----------------
const RATE_MAX_AGE = 12 * 3600 * 1000;
let rateRequest = null;

function cachedRate() {
  const c = readJSON("localStorage", RATE_KEY, null);
  return c && Number.isFinite(c.rate) && c.rate > 0 && Number.isFinite(c.at) ? c : null;
}

/** { rate, at } или null: если курс ни разу не получен, суммы показываются только в $. */
export async function ensureRate() {
  const cached = cachedRate();
  if (cached && Date.now() - cached.at < RATE_MAX_AGE) return cached;
  rateRequest ??= (async () => {
    try {
      const r = await fetch("https://open.er-api.com/v6/latest/USD", { referrerPolicy: "no-referrer" });
      const rate = Number((await r.json()).rates.KZT);
      if (!Number.isFinite(rate) || rate <= 0) return cached;
      const fresh = { rate, at: Date.now() };
      writeItem("localStorage", RATE_KEY, fresh);
      return fresh;
    } catch {
      return cached;
    } finally {
      rateRequest = null;
    }
  })();
  return rateRequest;
}
export const formatNumber = (v) => Math.round(v).toLocaleString("ru-RU");
export const formatUsd = (v) => `${formatNumber(v)} $`;
export const formatKzt = (usd, rate) => (rate ? `${formatNumber(Math.round((usd * rate.rate) / 100) * 100)} ₸` : "");
export function describeRate(rate) {
  if (!rate) return "Курс тенге сейчас недоступен.";
  return `Курс: 1 $ = ${rate.rate.toFixed(2)} ₸ от ${new Date(rate.at).toLocaleDateString("ru-RU")} (Exchange Rate API).`;
}

// ---------------- Консультант ----------------
function tripContext(s) {
  return `Сегодня: ${new Date().toISOString().slice(0, 10)}\nГород вылета: ${s.departureCity || "не указан"}\n`
    + `Гражданство: ${s.citizenship || "не указано"}\nПутешественников: ${s.travelers}`;
}

export const CONSULTANT_GREETING = "Здравствуйте! Я ИИ-консультант по путешествиям. Спросите, куда поехать, сколько это будет "
  + "стоить, какая нужна виза, что взять с собой или как составить маршрут.";
export const CONSULTANT_SUGGESTIONS = [
  "Куда поехать на море зимой недорого?", "Составь маршрут на 5 дней по Стамбулу",
  "Сколько стоит неделя в Грузии на двоих?", "Что нужно для визы в Шенген?",
];

const CONSULTANT_PROMPT = `Ты - ИИ-консультант по путешествиям внутри приложения «Выбор направления для путешествия».

- Отвечай по-русски, дружелюбно и по делу. Обычно хватает 1-3 коротких абзацев или списка; длиннее -
  только когда просят маршрут или подробный план. Можно использовать простой Markdown, без таблиц и картинок.
- Говори только о путешествиях: направления, маршруты, бюджет, визы, транспорт, жильё, климат, культура,
  безопасность, сборы. На другие темы вежливо откажись одной фразой и предложи вернуться к поездкам.
- Если не хватает данных (даты, бюджет, состав), дай ориентир и задай один-два уточняющих вопроса.
- Когда речь о стоимости, называй итоговую сумму в $ и в тенге (если известен курс) и короткую разбивку.
  Это оценка: ты не видишь реальных цен и наличия мест, забронировать ничего не можешь.
- Тебя зовут «ИИ-консультант». Если спросят, на какой технологии ты работаешь, скажи, что это вопрос к
  разработчику приложения; ничего об этом не выдумывай.
- Не выдумывай рейсы, отели и ссылки. Визовые правила и вопросы безопасности советуй проверить в
  официальных источниках.`;

/** discuss - чат открыт с экрана результата, то есть разговор именно об этом подборе. */
export function buildConsultantMessages(history, rate, tripSummary, discuss = false) {
  let system = `${CONSULTANT_PROMPT}\n\n${tripContext(getSettings())}`;
  if (rate) system += `\nКурс: 1 USD = ${rate.rate.toFixed(2)} KZT`;
  if (tripSummary) {
    system += `\n\nПоследний подбор пользователя в приложении:\n${tripSummary}`;
    if (discuss) {
      system += "\n\nПользователь открыл чат, чтобы обсудить именно этот подбор. Если место не названо, речь о нём, "
        + "даже если раньше в переписке обсуждали другое. Не переспрашивай то, что уже указано выше.";
    }
  }
  return [{ role: "system", content: system }, ...history.slice(-20).map(({ role, content }) => ({ role, content }))];
}

/** Полное описание подбора - контекст для консультанта. */
export function summarizeResult(r) {
  const { cost, criteria } = r;
  const lines = [
    `Направление: ${r.country} - ${r.city}. ${r.description}`,
    `Оценка стоимости: ${formatUsd(cost.total)} на ${cost.travelers} чел. (${formatUsd(cost.perPerson)} на человека), ночей: ${cost.nights}`,
    ...cost.items.map((i) => `- ${i.category}: ${formatUsd(i.amount)}`),
  ];
  const asked = criteria.filter((c) => c.status !== "any");
  if (asked.length) lines.push("Пожелания: " + asked.map((c) => `${c.title.toLowerCase()} - ${c.wanted}`).join("; "));
  lines.push("Место по базе приложения: " + criteria.map((c) => `${c.title.toLowerCase()} - ${c.actual}`).join("; "));
  const compromises = asked.filter((c) => c.status !== "match").map((c) => c.title.toLowerCase());
  if (compromises.length) lines.push("Не совпало полностью: " + compromises.join(", "));
  if (r.refinements?.length) lines.push("Уточнения пользователя: " + r.refinements.join("; "));
  if (r.confidence !== undefined) lines.push(`Соответствие запросу по расчёту приложения: ${r.confidence}%`);
  if (r.alternatives?.length) lines.push("Запасные варианты: " + r.alternatives.map((a) => `${a.country} - ${a.city} (${formatUsd(a.total)})`).join("; "));
  return lines.join("\n");
}

/** Первое сообщение чата, открытого с экрана результата (собирается на месте, без запроса к ИИ). */
export function resultIntro(r, rate) {
  const { cost } = r;
  const kzt = formatKzt(cost.total, rate);
  const lines = [
    `Вижу ваш подбор: **${r.country} — ${r.city}**. ${r.description}`, "",
    `- Сумма: ≈ ${formatUsd(cost.total)}${kzt ? ` (${kzt})` : ""} на ${cost.travelers} чел., ночей: ${cost.nights}`,
  ];
  const asked = r.criteria.filter((c) => c.status !== "any").map((c) => c.wanted);
  if (asked.length) lines.push("- Ваш запрос: " + asked.join(", "));
  if (r.refinements?.length) lines.push("- Уточнения: " + r.refinements.join("; "));
  if (r.confidence !== undefined) lines.push(`- Соответствие запросу: ${r.confidence}%`);
  if (r.alternatives?.length) lines.push("- Запасные варианты: " + r.alternatives.map((a) => `${a.country} — ${a.city}`).join(", "));
  return lines.concat("", "Что обсудим?").join("\n");
}

export function resultSuggestions(r) {
  return [
    `Составь маршрут: ${r.country}, ${r.city}, ночей: ${r.cost.nights}`,
    `Хватит ли ${formatUsd(r.cost.total)} на эту поездку?`,
    "Нужна ли мне виза и какие документы?",
    "Когда лучше ехать и что взять с собой?",
  ];
}
