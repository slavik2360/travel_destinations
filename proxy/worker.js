// Прокси для ИИ-консультанта (Cloudflare Workers). Ключ API хранится здесь как секрет и никогда не
// попадает к пользователям: приложение обращается к прокси без ключа.
//
// Адрес прокси виден всем, поэтому он ограничивает то, что через него можно потратить: одна
// фиксированная модель, предел размера запроса и ответа, лимит запросов с одного IP. Обязательно
// задайте ещё и месячный лимит расходов в кабинете провайдера API.

// Режимы ИИ, как в приложении. Какой включён, задаёт переменная PROVIDER в wrangler.toml.
const PROVIDERS = {
  1: { url: "https://api.openai.com/v1/chat/completions", model: "gpt-6-luna", fallbacks: [], secret: "AI_API_KEY_1" },
  2: {
    url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    model: "gemini-3.5-flash-lite",
    fallbacks: ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"], secret: "AI_API_KEY_2",
  },
};
const MAX_BODY_BYTES = 64 * 1024;
const MAX_MESSAGES = 60;
const MAX_OUTPUT_TOKENS = 6000;

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim());
  const headers = { Vary: "Origin" };
  if (origin && allowed.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Methods"] = "POST, OPTIONS";
    headers["Access-Control-Allow-Headers"] = "authorization, content-type";
    headers["Access-Control-Max-Age"] = "86400";
  }
  return headers;
}

const json = (status, data, headers) =>
  new Response(JSON.stringify(data), { status, headers: { ...headers, "Content-Type": "application/json" } });
const fail = (status, code, headers) => json(status, { error: { code, message: code } }, headers);

export default {
  async fetch(request, env) {
    const cors = corsHeaders(request, env);
    const { pathname } = new URL(request.url);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    // У браузера чужой сайт отсекаем сразу; у десктопного приложения заголовка Origin нет.
    if (request.headers.get("Origin") && !cors["Access-Control-Allow-Origin"]) return fail(403, "origin_not_allowed", cors);
    if (request.method !== "POST" || pathname !== "/v1/chat/completions") return fail(404, "not_found", cors);

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.RATE_LIMITER.limit({ key: ip });
    if (!success) return fail(429, "rate_limit", cors);

    const text = await request.text();
    if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) return fail(413, "request_too_large", cors);
    let input;
    try { input = JSON.parse(text); } catch { return fail(400, "bad_json", cors); }
    const messages = input?.messages;
    const valid = Array.isArray(messages) && messages.length > 0 && messages.length <= MAX_MESSAGES
      && messages.every((m) => m && ["system", "user", "assistant"].includes(m.role) && typeof m.content === "string");
    if (!valid) return fail(400, "bad_messages", cors);

    const provider = PROVIDERS[env.PROVIDER];
    if (!provider || !env[provider.secret]) {
      console.error("proxy is not configured: PROVIDER", env.PROVIDER);
      return fail(503, "not_configured", cors);
    }
    // Собираем запрос заново: из присланного берём только то, что нужно приложению.
    const body = { messages, max_completion_tokens: MAX_OUTPUT_TOKENS, stream: input.stream === true };

    const ask = (model) => fetch(provider.url, {
      method: "POST",
      headers: { Authorization: `Bearer ${env[provider.secret]}`, "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, model }),
    });
    let upstream;
    try {
      upstream = await ask(provider.model);
      for (const model of provider.fallbacks) {
        // Основная модель перегружена или исчерпала лимит: берём первую запасную, которая отвечает,
        // иначе остаёмся с исходной ошибкой.
        if (upstream.status < 500 && upstream.status !== 429) break;
        const next = await ask(model);
        if (next.ok) upstream = next;
      }
    } catch {
      return fail(502, "upstream_unavailable", cors);
    }
    if (!upstream.ok) {
      // Текст ошибки провайдера наружу не отдаём (в нём бывает часть ключа) - он в логах воркера.
      const detail = await upstream.text();
      console.error("upstream", upstream.status, detail.slice(0, 500));
      if (upstream.status === 429 && /per.?day/i.test(detail)) return fail(429, "limit_per_day", cors);
      if (upstream.status === 429) return fail(429, /"(code|type)":\s*"[^"]*(quota|credit|billing|spend_limit|usage_limit)/.test(detail) ? "insufficient_quota" : "rate_limit", cors);
      return fail(upstream.status >= 500 ? 503 : 502, "upstream_error", cors);
    }
    return new Response(upstream.body, {
      status: 200,
      headers: { ...cors, "Content-Type": upstream.headers.get("Content-Type") || "application/json", "Cache-Control": "no-store" },
    });
  },
};
