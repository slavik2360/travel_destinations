// Экраны ИИ: меню, консультант, настройки и карточка стоимости. Всё, что пришло от модели,
// проходит через escapeHtml ДО любой разметки: на странице хранится API-ключ, поэтому чужой HTML
// здесь недопустим.
import {
  CONSULTANT_GREETING, CONSULTANT_SUGGESTIONS, PROVIDERS,
  buildConsultantMessages, checkConnection, describeRate, ensureRate, formatKzt, formatUsd, getChat,
  getSettings, isConfigured, resultIntro, resultSuggestions, saveChat, saveSettings, streamChat, summarizeResult, validateBaseUrl,
} from "./ai.js";
import { BUDGET_STATUS_TEXT } from "./smart.js";

export function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Минимальный Markdown поверх уже экранированного текста: заголовки, списки, жирный, код. */
export function renderMarkdown(source) {
  const inline = (s) => s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of escapeHtml(source).split("\n")) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const ordered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (bullet || ordered) {
      const tag = bullet ? "ul" : "ol";
      if (list !== tag) { close(); out.push(ordered ? `<ol start="${Number(ordered[1])}">` : "<ul>"); list = tag; }
      out.push(`<li>${inline(bullet ? bullet[1] : ordered[2])}</li>`);
    } else if (line.trim() || !list) {
      close();
      if (heading) out.push(`<p><b>${inline(heading[1])}</b></p>`);
      else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
  }
  close();
  return out.join("");
}

// ---------------- Стоимость ----------------
export function costCardHtml(cost) {
  if (!cost) return "";
  const details = [`на ${cost.travelers} чел.`];
  if (cost.nights) details.push(`ночей: ${cost.nights}`);
  if (cost.travelers > 1) details.push(`≈ ${formatUsd(cost.perPerson)} на человека`);
  const rows = cost.items.map((i) => `<div class="cost-row"><span>${escapeHtml(i.category)}</span><b>${formatUsd(i.amount)}</b></div>`).join("");
  return `
    <div class="cost-card">
      <div class="meta">💵 Сумма путешествия</div>
      <div class="cost-total">≈ ${formatUsd(cost.total)}<span id="cost-kzt"></span></div>
      <div class="meta">${details.join(" · ")}</div>
      ${cost.budgetStatus ? `<div class="cost-status">${BUDGET_STATUS_TEXT[cost.budgetStatus]}</div>` : ""}
      ${rows}
      <div class="cost-note">Ориентир: уровень цен направления, длительность и типичные доли расходов. Перед покупкой сверьтесь с реальными ценами.</div>
      <div class="cost-note" id="cost-rate"></div>
    </div>`;
}
export function wireCostCard(cost) {
  if (!cost) return;
  const kzt = document.getElementById("cost-kzt"), note = document.getElementById("cost-rate");
  ensureRate().then((rate) => {
    if (!kzt.isConnected) return;
    if (rate) kzt.textContent = ` · ${formatKzt(cost.total, rate)}`;
    note.textContent = describeRate(rate);
  });
}

// ---------------- Экраны ----------------
// ctx: { state, go(screen), startWizard(useAi) }
export function createAiScreens(ctx) {
  const { state, go } = ctx;
  const $ = (id) => document.getElementById(id);

  // ----- меню -----
  function renderAiMenu() {
    return `
      <button class="gear" id="ai-settings-btn" title="Параметры" aria-label="Параметры">⚙</button>
      <div class="icon-xl">🤖</div>
      <h1 class="title">ИИ для путешествий</h1>
      <p class="subtitle">${isConfigured() ? "Консультант подключён и готов к работе" : "Консультант не подключён, подбор работает"}</p>
      <button class="btn" id="chat-btn">💬 ИИ-консультант</button>
      <button class="btn" id="ai-wizard-btn">🧭 Подбор путешествия с ИИ</button>
      <button class="btn no" id="back-btn">Назад</button>`;
  }
  function wireAiMenu() {
    $("chat-btn").addEventListener("click", () => { state.chatBack = "aiMenu"; go("consultant"); });
    $("ai-wizard-btn").addEventListener("click", () => ctx.startWizard(true));
    $("ai-settings-btn").addEventListener("click", () => go("aiSettings"));
    $("back-btn").addEventListener("click", () => go("menu"));
  }

  // ----- настройки -----
  function renderAiSettings() {
    const s = getSettings();
    return `
      <div class="top-bar"><h2>Параметры поездки</h2></div>
      <p class="subtitle">Нужны для суммы поездки и советов консультанта. Подбор работает прямо в браузере, а сообщения консультанту вместе с этими данными и последним подбором обрабатываются на внешнем сервере.</p>
      <div class="field"><label for="s-city">Город вылета</label><input id="s-city" value="${escapeHtml(s.departureCity)}"></div>
      <div class="field"><label for="s-citizen">Гражданство</label><input id="s-citizen" value="${escapeHtml(s.citizenship)}"></div>
      <div class="field"><label for="s-travelers">Путешественников</label><input id="s-travelers" type="number" min="1" max="20" value="${s.travelers}"></div>
      <details class="more"><summary>Дополнительно</summary>
        <div class="field modes"><span>Режим</span>${Object.keys(PROVIDERS).map((n) =>
    `<label><input type="radio" name="s-provider" value="${n}" ${Number(n) === s.provider ? "checked" : ""}> ${n}</label>`).join("")}</div>
        <div class="field"><label for="s-key">Ключ доступа</label><input id="s-key" type="password" autocomplete="off" value="${escapeHtml(s.keys[s.provider])}"></div>
        <label class="check"><input type="checkbox" id="s-remember" ${s.remember ? "checked" : ""}> Запомнить ключ на этом устройстве (не включайте на чужом компьютере)</label>
        <div class="field"><label for="s-url">Сервер</label><input id="s-url" placeholder="По умолчанию" value="${escapeHtml(s.baseUrl)}"></div>
        <button class="btn secondary" id="s-check">Проверить подключение</button>
      </details>
      <p class="status-note" id="s-status"></p>
      <div class="row"><button class="btn no" id="s-cancel">Отмена</button><button class="btn" id="s-save">Сохранить</button></div>`;
  }
  function wireAiSettings() {
    const status = (msg, ok) => { const el = $("s-status"); if (el) { el.textContent = msg; el.className = `status-note ${ok ? "ok" : "bad"}`; } };
    // У каждого режима свой ключ: при переключении поле показывает ключ выбранного режима.
    const { keys, provider: initial } = getSettings();
    let provider = initial;
    document.querySelectorAll("[name=s-provider]").forEach((radio) => radio.addEventListener("change", () => {
      keys[provider] = $("s-key").value.trim();
      provider = Number(radio.value);
      $("s-key").value = keys[provider];
      status("", true);
    }));
    const collect = () => {
      keys[provider] = $("s-key").value.trim();
      const v = {
        provider, keys: { ...keys }, remember: $("s-remember").checked,
        baseUrl: $("s-url").value.trim().replace(/\/+$/, ""),
        departureCity: $("s-city").value.trim(), citizenship: $("s-citizen").value.trim(),
        travelers: Math.min(20, Math.max(1, parseInt($("s-travelers").value, 10) || 1)),
      };
      const error = v.baseUrl ? validateBaseUrl(v.baseUrl) : null;
      if (error) { status(error, false); return null; }
      return v;
    };
    $("s-check").addEventListener("click", async (e) => {
      const v = collect();
      if (!v) return;
      const btn = e.currentTarget;
      btn.disabled = true;
      status("Проверяю подключение...", true);
      try {
        await checkConnection(v);
        status("Подключение работает, ИИ отвечает.", true);
      } catch (err) {
        status(err.message, false);
      }
      btn.disabled = false;
    });
    $("s-cancel").addEventListener("click", () => go("aiMenu"));
    $("s-save").addEventListener("click", () => {
      const v = collect();
      if (!v) return;
      saveSettings(v);
      go("aiMenu");
    });
  }

  // ----- консультант -----
  let chatAbort = null;

  const bubble = (role, text) => role === "user"
    ? `<div class="bubble user">${escapeHtml(text)}</div>`
    : `<div class="bubble">${renderMarkdown(text)}</div>`;

  const isDiscuss = () => state.chatBack === "result" && Boolean(state.lastResult);

  function renderConsultant() {
    const chat = getChat();
    const r = state.lastResult;
    // Чат открыт с экрана результата: сразу показываем подбор и вопросы именно о нём. Карточка идёт
    // после прежней переписки: так видно, что дальше речь о новом подборе.
    const discuss = isDiscuss();
    const bubbles = chat.map((m) => bubble(m.role, m.content)).join("");
    const suggestions = discuss ? resultSuggestions(r) : CONSULTANT_SUGGESTIONS;
    return `
      <div class="top-bar"><h2>💬 ИИ-консультант</h2>
        <button class="btn secondary small" id="chat-new">Новый диалог</button></div>
      ${r && !discuss ? `<p class="meta-line">Консультант знает о вашем подборе: ${escapeHtml(r.country)} — ${escapeHtml(r.city)}</p>` : ""}
      <div class="chat" id="chat">${discuss
    ? `${bubbles}<div class="bubble" id="chat-intro">${renderMarkdown(resultIntro(r, null))}</div>`
    : bubble("assistant", CONSULTANT_GREETING) + bubbles}</div>
      <div id="chat-chips" ${discuss || !chat.length ? "" : "hidden"}>${suggestions.map((t) => `<button class="chip">${escapeHtml(t)}</button>`).join("")}</div>
      <p class="status-note bad" id="chat-error"></p>
      <div class="chat-input"><textarea id="chat-text" rows="2" placeholder="Спросите о поездке..."></textarea>
        <button class="btn" id="chat-send">Отправить</button></div>
      <p class="cost-note center">Не указывайте личные данные: сообщения обрабатываются на внешнем сервере.</p>
      <button class="btn no" id="back-btn">Назад</button>`;
  }

  function wireConsultant() {
    const box = $("chat"), input = $("chat-text"), sendBtn = $("chat-send"), errorEl = $("chat-error"), chips = $("chat-chips");
    const toBottom = () => window.scrollTo(0, document.body.scrollHeight);
    const discuss = isDiscuss();
    let asked = false;
    const result = state.lastResult;
    ensureRate().then((rate) => {
      const intro = $("chat-intro");
      if (intro && rate && state.lastResult === result) intro.innerHTML = renderMarkdown(resultIntro(result, rate));
    });
    toBottom();

    const ask = async (question) => {
      question = question.trim();
      if (!question || chatAbort) return;
      if (!isConfigured()) { errorEl.textContent = "ИИ не подключён."; return; }
      const abort = new AbortController();
      chatAbort = abort;
      errorEl.textContent = "";
      chips.hidden = true;
      input.value = "";
      sendBtn.textContent = "Стоп";
      box.insertAdjacentHTML("beforeend", bubble("user", question) + bubble("assistant", "Печатает..."));
      const answerEl = box.lastElementChild, questionEl = answerEl.previousElementSibling;
      toBottom();

      let answer = "", frame = 0;
      const paint = () => {
        frame = 0;
        if (!answer) return;
        // Если пользователь отмотал вверх, не дёргаем его вниз при каждом куске ответа.
        const near = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
        answerEl.innerHTML = renderMarkdown(answer);
        if (near && answerEl.isConnected) toBottom();
      };
      const history = getChat().concat({ role: "user", content: question });
      try {
        const rate = await ensureRate();
        if (!abort.signal.aborted) {
          const summary = state.lastResult ? summarizeResult(state.lastResult) : "";
          const { truncated } = await streamChat(
            buildConsultantMessages(history, rate, summary, discuss),
            (piece) => { answer += piece; frame ||= requestAnimationFrame(paint); },
            abort.signal,
          );
          if (truncated) errorEl.textContent = "Ответ обрезан: модель достигла предела длины. Попросите продолжить.";
        }
      } catch (e) {
        if (!abort.signal.aborted) errorEl.textContent = e.message;
      }
      cancelAnimationFrame(frame);
      if (chatAbort === abort) chatAbort = null;
      if (abort.discard) return; // диалог очищен - ответ сохранять некуда
      paint();
      if (answer.trim()) {
        saveChat(history.concat({ role: "assistant", content: answer }));
        asked = true;
      } else {
        questionEl.remove();
        answerEl.remove();
        if (!input.value.trim()) input.value = question;
        chips.hidden = asked || (!discuss && getChat().length > 0);
      }
      sendBtn.textContent = "Отправить";
    };

    sendBtn.addEventListener("click", () => { if (chatAbort) chatAbort.abort(); else ask(input.value); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
    });
    chips.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
    $("chat-new").addEventListener("click", () => {
      if (getChat().length && !confirm("Удалить текущую переписку с консультантом?")) return;
      if (chatAbort) { chatAbort.discard = true; chatAbort.abort(); chatAbort = null; }
      saveChat([]);
      go("consultant");
    });
    const leave = (next) => { if (chatAbort) chatAbort.abort(); next(); };
    $("back-btn").addEventListener("click", () => leave(() => go(state.chatBack || "aiMenu")));
  }

  return {
    renderers: { aiMenu: renderAiMenu, aiSettings: renderAiSettings, consultant: renderConsultant },
    wirers: { aiMenu: wireAiMenu, aiSettings: wireAiSettings, consultant: wireConsultant },
  };
}
