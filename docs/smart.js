// Порт MyProgram/smart_engine.py: собственный ИИ подбора. Работает в браузере, без сети и внешних
// сервисов. Сравнивает с запросом все направления сразу (нечёткое соответствие с весами), объясняет
// расчёт и понимает уточнения обычным текстом. Правила и числа должны совпадать с Python-версией.

// [ключ, название, вес]
export const CRITERIA = [
  ["budget", "Бюджет", 6], ["continent", "Континент", 7], ["type", "Тип отдыха", 10],
  ["season", "Время года", 4], ["duration", "Продолжительность", 2], ["visa", "Виза", 3],
  ["interest", "Интересы", 2],
];
const TITLES = Object.fromEntries(CRITERIA.map(([key, title]) => [key, title]));

const BUDGET_ORDER = ["economy", "standard", "high", "luxury"];
const SEASON_ORDER = ["spring", "summer", "autumn", "winter"];
const DURATION_ORDER = ["short", "week", "long", "extended"];
const VISA_ORDER = ["visa_free", "easy_visa", "advance_visa"];

// Насколько один вариант заменяет другой, если точного совпадения нет.
const near = (pairs) => new Map(pairs.flatMap(([a, b, v]) => [[`${a}|${b}`, v], [`${b}|${a}`, v]]));
const NEAR_TYPES = near([
  ["nature", "active", 0.6], ["beach", "wellness", 0.4], ["nature", "wellness", 0.35],
  ["nature", "excursion", 0.3], ["active", "excursion", 0.2],
]);
const NEAR_CONTINENTS = near([
  ["europe", "asia", 0.2], ["europe", "africa", 0.2], ["asia", "africa", 0.15],
  ["asia", "australia_oceania", 0.2], ["north_america", "south_america", 0.2],
]);
// «any» у направления - в базе нет ограничения, но и нет подтверждения, что оно именно для этого.
const UNSPECIFIED = { season: 0.95, duration: 0.9, visa: 0.85, interest: 0.8 };

/** Степень соответствия 0..1 или null, если критерий пользователю не важен. */
export function degree(key, wanted, actual) {
  if (wanted === "any") return null;
  if (actual === wanted) return 1;
  if (actual === "any") return UNSPECIFIED[key] ?? 0.8;
  const index = (order) => [order.indexOf(wanted), order.indexOf(actual)];
  if (key === "type") return NEAR_TYPES.get(`${wanted}|${actual}`) ?? 0;
  if (key === "continent") return NEAR_CONTINENTS.get(`${wanted}|${actual}`) ?? 0;
  if (key === "budget") {
    const [w, a] = index(BUDGET_ORDER);
    const step = a - w;
    return step < 0 ? Math.max(0, 1 + step * 0.35) : Math.max(0, 0.2 - (step - 1) * 0.2);
  }
  if (key === "season") {
    const [w, a] = index(SEASON_ORDER);
    return (w - a) % 2 ? 0.4 : 0;
  }
  if (key === "duration") {
    const [w, a] = index(DURATION_ORDER);
    return Math.max(0, 1 - 0.5 * Math.abs(w - a));
  }
  if (key === "visa") {
    const [w, a] = index(VISA_ORDER);
    // Виза проще, чем готов оформлять человек, - почти то же самое; сложнее - уже препятствие.
    return a < w ? 0.95 : 0.2;
  }
  return key === "interest" ? 0.2 : 0;
}

function criterionStatus(value) {
  if (value === null) return "any";
  if (value >= 0.8) return "match";
  return value > 0.2 ? "partial" : "mismatch";
}

/** Сверка направления с ответами по каждому критерию. */
export function compareWithQuery(dest, query, names) {
  return CRITERIA.map(([key, title, weight]) => {
    const value = degree(key, query[key], dest[key]);
    return {
      key, title, weight, degree: value, status: criterionStatus(value),
      wanted: names[key][query[key]] ?? query[key], actual: names[key][dest[key]] ?? dest[key],
    };
  });
}

/** Общее соответствие 0..1: взвешенное среднее по важным для пользователя критериям. */
export function score(dest, query) {
  let total = 0, earned = 0;
  for (const [key, , weight] of CRITERIA) {
    const value = degree(key, query[key], dest[key]);
    if (value === null) continue;
    total += weight;
    earned += weight * value;
  }
  return total ? earned / total : 1;
}

/** [сколько критериев выполнено полностью или частично, сколько пользователь задал]. */
export function countMatches(criteria) {
  const asked = criteria.filter((c) => c.status !== "any");
  return [asked.filter((c) => c.status !== "mismatch").length, asked.length];
}

// ---------------- Стоимость ----------------
const BUDGET_LIMITS = { economy: [0, 500], standard: [500, 1500], high: [1500, 4000], luxury: [4000, null] };
const COST_RANGE = { economy: [250, 500], standard: [500, 1500], high: [1500, 4000], luxury: [4000, 9000] };
const DURATION_SHARE = { short: 0.15, week: 0.45, long: 0.75, extended: 1 };
const NIGHTS = { short: 3, week: 7, long: 13, extended: 20 };
// Доля дороги в поездке: чем дальше континент, тем больше.
const ROAD_SHARE = {
  asia: 0.30, europe: 0.30, africa: 0.36, north_america: 0.45, south_america: 0.45,
  australia_oceania: 0.45, antarctica: 0.55,
};
// Как обычно делятся остальные деньги: проживание, питание, транспорт, развлечения, прочее.
const SPLIT_NAMES = ["Проживание", "Питание", "Транспорт на месте", "Экскурсии и развлечения", "Виза, страховка и прочее"];
const SPLIT = {
  beach: [0.42, 0.26, 0.10, 0.14, 0.08], excursion: [0.36, 0.24, 0.12, 0.20, 0.08],
  nature: [0.36, 0.24, 0.12, 0.20, 0.08], active: [0.32, 0.22, 0.12, 0.26, 0.08],
  wellness: [0.46, 0.22, 0.06, 0.18, 0.08],
};
export const BUDGET_STATUS_TEXT = {
  within: "✅ Укладывается в выбранный бюджет",
  over: "⚠️ Выше выбранного бюджета",
  below: "💚 Дешевле выбранного бюджета",
};
const round10 = (v) => Math.floor(v / 10 + 0.5) * 10;

function budgetStatus(code, perPerson) {
  if (!BUDGET_LIMITS[code]) return null;
  const [low, high] = BUDGET_LIMITS[code];
  if (high !== null && perPerson > high) return "over";
  return perPerson < low ? "below" : "within";
}

function tripDuration(dest, query) {
  const duration = query.duration !== "any" ? query.duration : dest.duration;
  return NIGHTS[duration] ? duration : "week";
}

export function perPersonCost(dest, query) {
  const [low, high] = COST_RANGE[dest.budget] ?? [500, 1500];
  return round10(low + (high - low) * DURATION_SHARE[tripDuration(dest, query)]);
}

/** Уровень цен направления, сдвинутый по длительности; статьи складываются ровно в итог. */
export function estimateCost(dest, query, travelers) {
  const perPerson = perPersonCost(dest, query);
  const total = perPerson * travelers;
  const road = round10(total * (ROAD_SHARE[dest.continent] ?? 0.35));
  const shares = SPLIT[dest.type] ?? SPLIT.beach;
  const items = [{ category: "Перелёт и дорога", amount: road }];
  for (let i = 1; i < shares.length; i++) items.push({ category: SPLIT_NAMES[i], amount: round10((total - road) * shares[i]) });
  // Остаток округления - в самую крупную статью.
  items.splice(1, 0, { category: SPLIT_NAMES[0], amount: total - items.reduce((s, i) => s + i.amount, 0) });
  return {
    total, perPerson, travelers, nights: NIGHTS[tripDuration(dest, query)], items,
    budgetStatus: budgetStatus(query.budget, perPerson),
  };
}

// ---------------- Уточнения текстом ----------------
export const emptyFilters = () => ({
  maxTotal: null, maxPerPerson: null, onlyCountries: [], noCountries: [],
  noContinents: [], noTypes: [], noPlaces: [], quiet: false,
});

// В JavaScript \b и \w не знают кириллицу, поэтому подменяем их своими.
const W = "[а-яёa-z0-9_]";
const EDGE = `(?:(?<!${W})(?=${W})|(?<=${W})(?!${W}))`;
const rx = (source, flags = "") => new RegExp(source.replaceAll("\\b", EDGE).replaceAll("\\w", W), flags);
const escapeRx = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Порядок важен: более конкретные фразы - раньше.
const KEYWORDS = [
  ["visa", "visa_free", "без\\s+виз\\w*|безвиз\\w*"],
  ["visa", "easy_visa", "виз\\w*\\s+по\\s+прил[её]т\\w*|электронн\\w+\\s+виз\\w*"],
  ["type", "beach", "пляж\\w*|мор(?:е|я|ю|ем)|купа(?:ть|ни)\\w*|океан(?:а|е|у|ом)?"],
  ["type", "excursion", "экскурс\\w*|музе\\w*|архитектур\\w*|достопримечательност\\w*"],
  ["type", "active", "активн\\w*|треккинг\\w*|поход\\w*|лыж\\w*|с[её]рф\\w*|дайв\\w*|спорт\\w*|экстрим\\w*"],
  ["type", "nature", "природ\\w*|гор(?:ы|ах|ам|ами)|лес(?:а|ах|у)?|оз[её]р\\w*"],
  ["type", "wellness", "спа|оздоров\\w*|массаж\\w*|термальн\\w*|релакс\\w*|санатор\\w*"],
  ["season", "spring", "весн\\w*|март\\w*|апрел\\w*|ма(?:й|е|я)"],
  ["season", "summer", "лет(?:о|ом)|летн\\w+|июн\\w*|июл\\w*|август\\w*"],
  ["season", "autumn", "осен\\w*|сентябр\\w*|октябр\\w*|ноябр\\w*"],
  ["season", "winter", "зим\\w*|декабр\\w*|январ\\w*|феврал\\w*"],
  ["duration", "short", "выходн\\w*|уик-?энд\\w*"],
  ["duration", "extended", "месяц\\w*"],
  ["interest", "nightlife", "ночн\\w+\\s+жизн\\w*|клуб\\w*|вечерин\\w*|тусов\\w*"],
  ["interest", "gastronomy", "ед(?:а|ы|ой)|кухн\\w*|гастроном\\w*|ресторан\\w*"],
  ["interest", "family", "дет(?:и|ей|ьми|ям)|детск\\w+|реб[её]н\\w*|семь(?:я|и|ю|ей)|семейн\\w*"],
  ["interest", "romance", "романти\\w*|медов\\w+|девушк\\w*|жен(?:ой|а)|мужем|вдво[её]м"],
  ["interest", "shopping", "шопп?инг\\w*|покупк\\w*|магазин\\w*"],
  ["budget", "luxury", "люкс\\w*|роскош\\w*"],
  ["continent", "asia", "ази(?:я|и|ю|ей)"],
  ["continent", "africa", "африк\\w+"],
  ["continent", "europe", "европ\\w+"],
  ["continent", "north_america", "северн\\w+\\s+америк\\w+"],
  ["continent", "south_america", "южн\\w+\\s+америк\\w+|латинск\\w+\\s+америк\\w+"],
  ["continent", "australia_oceania", "океани(?:я|и|ю|ей)"],
  ["continent", "antarctica", "антарктид\\w+"],
].map(([key, value, source]) => [key, value, rx(`\\b(?:${source})\\b`)]);

const CHEAPER = rx("\\b(?:дешевл\\w*|подешевл\\w*|бюджетн\\w+|эконом\\w*|недорог\\w*|дорого)\\b");
const PRICIER = rx("\\b(?:дороже|подороже|шикарн\\w*)\\b");
const OTHER_COUNTRY = rx("\\bдруг\\w+\\s+стран\\w+|\\bв\\s+другую\\b");
const OTHER_PLACE = rx("\\bдруг\\w+\\s+(?:вариант\\w*|мест\\w*|город\\w*|направлен\\w*)|\\bещ[её]\\s+вариант\\w*|\\bне\\s+нравится\\b");
const QUIET = rx("\\b(?:спокойн\\w*|тих(?:о|ое|ий|ую|ого)|потише|меньше\\s+турист\\w*|уедин\\w*)\\b");
const MONEY = rx("(?<num>\\d[\\d\\s]*(?:[.,]\\d+)?)\\s*(?<k>тыс\\w*\\.?|к\\b|млн|миллион\\w*)?\\s*(?<cur>\\$|usd|доллар\\w*|бакс\\w*|₸|тенге|тг\\b|kzt)", "g");
// Сумма без валюты - только после явного «не больше / до / максимум»; считаем её долларами.
const BARE_MONEY = rx("\\b(?:не\\s+больше|не\\s+дороже|не\\s+более|максимум|дешевле|до)\\s+(\\d[\\d\\s]*\\d)(?!\\s*(?:дн|ноч|сут|чел))");
const DAYS = rx("(\\d{1,2})\\s*(?:-\\s*\\d{1,2}\\s*)?(?:дн\\w*|день|ноч\\w*|сут\\w*)");
const WEEKS = rx("(?:(\\d|две|три)\\s+)?недел\\w+");
const NEGATION = rx("\\b(не|без|кроме|ни)\\b(?!\\s+только)[^,.;!?]{0,18}$");

function countryPattern(country) {
  const words = country.toLowerCase().replaceAll("ё", "е").split(/[\s-]+/).map((word) => {
    const cut = /(?:ая|яя|ый|ий|ое|ие|ые)$/.test(word) ? 2 : 1;
    return word.length <= 4 ? escapeRx(word) : `${escapeRx(word.slice(0, -cut))}[а-я]{0,3}`;
  });
  return rx(`\\b${words.join("[\\s-]+")}\\b`);
}

const spaced = (v) => Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");

/** Разбирает уточнение. Возвращает { query, filters, notes }; пустой notes - не разобрано. */
export function parseWish(text, oldQuery, oldFilters, current, countries, usdToKzt, names) {
  const query = { ...oldQuery };
  const filters = structuredClone(oldFilters);
  const low = text.toLowerCase().replaceAll("ё", "е");
  const notes = [];
  const used = [];
  const take = (m) => used.push([m.index, m.index + m[0].length]);
  const free = (m) => used.every(([a, b]) => m.index + m[0].length <= a || m.index >= b);
  const negated = (start) => NEGATION.test(low.slice(0, start));
  const titled = (key) => TITLES[key].toLowerCase();

  for (const m of low.matchAll(MONEY)) {
    take(m);
    let value = Number(m.groups.num.replace(/\s/g, "").replace(",", "."));
    const factor = m.groups.k ?? "";
    value *= /^(млн|миллион)/.test(factor) ? 1e6 : factor ? 1000 : 1;
    if (!/^(\$|usd|доллар|бакс)/.test(m.groups.cur)) {
      if (!usdToKzt) { notes.push("сумму в тенге учесть не могу: курс недоступен"); continue; }
      value /= usdToKzt;
    }
    if (value > 0) {
      filters.maxTotal = value;
      notes.push(`вся поездка не дороже ${spaced(value)} $`);
    }
  }
  let moneyGiven = used.length > 0;
  const bare = moneyGiven ? null : low.match(BARE_MONEY);
  if (bare) {
    const value = Number(bare[1].replace(/\s/g, ""));
    if (value >= 100) {
      moneyGiven = true;
      take(bare);
      filters.maxTotal = value;
      notes.push(`вся поездка не дороже ${spaced(value)} $`);
    }
  }

  const weeks = low.match(WEEKS), days = low.match(DAYS);
  let number = null;
  if (days) { number = Number(days[1]); take(days); }
  else if (weeks) { number = 7 * ({ две: 2, три: 3 }[weeks[1]] ?? Number(weeks[1] ?? 1)); take(weeks); }
  if (number) {
    query.duration = number <= 4 ? "short" : number <= 9 ? "week" : number <= 16 ? "long" : "extended";
    notes.push(`${titled("duration")}: ${names.duration[query.duration]}`);
  }

  for (const country of [...countries].sort((a, b) => b.length - a.length)) {
    const m = low.match(countryPattern(country));
    if (!m || !free(m)) continue;
    take(m);
    if (negated(m.index)) {
      filters.noCountries.push(country);
      filters.onlyCountries = filters.onlyCountries.filter((c) => c !== country);
      notes.push(`кроме страны ${country}`);
    } else {
      if (!notes.some((n) => n.startsWith("страна: "))) filters.onlyCountries = [];
      filters.onlyCountries.push(country);
      filters.noCountries = filters.noCountries.filter((c) => c !== country);
      query.continent = "any"; // названная страна важнее континента из анкеты
      notes.push(`страна: ${country}`);
    }
  }

  for (const [key, value, pattern] of KEYWORDS) {
    const m = low.match(pattern);
    if (!m || !free(m)) continue;
    take(m);
    const label = names[key][value];
    // «без визы» само содержит «без» - это не отрицание.
    if (key !== "visa" && negated(m.index)) {
      if (key === "type") filters.noTypes.push(value);
      else if (key === "continent") filters.noContinents.push(value);
      if (query[key] === value) query[key] = "any";
      else if (key !== "type" && key !== "continent") continue;
      notes.push(`кроме «${label}»`);
    } else {
      query[key] = value;
      if (key === "type") filters.noTypes = filters.noTypes.filter((t) => t !== value);
      else if (key === "continent") {
        filters.noContinents = filters.noContinents.filter((c) => c !== value);
        filters.onlyCountries = [];
      }
      notes.push(`${titled(key)}: ${label}`);
    }
  }

  const cheaper = low.match(CHEAPER);
  let pricier = low.match(PRICIER);
  if (pricier && negated(pricier.index)) pricier = null;
  if (cheaper && !moneyGiven) { // «слишком дорого» и «недорого» - одна и та же просьба
    filters.maxPerPerson = current.cost.perPerson - 1;
    notes.push(`дешевле ${spaced(current.cost.perPerson)} $ на человека`);
  } else if (pricier) {
    query.budget = BUDGET_ORDER[Math.min(BUDGET_ORDER.indexOf(current.destination.budget) + 1, 3)];
    filters.maxPerPerson = null;
    filters.maxTotal = null;
    notes.push(`бюджет: ${names.budget[query.budget]}`);
  }
  if (cheaper || moneyGiven) query.budget = "any"; // иначе критерий «Бюджет» тянул бы к прежнему уровню

  const place = `${current.country}|${current.city}`;
  if (OTHER_COUNTRY.test(low)) {
    filters.noCountries.push(current.country);
    filters.onlyCountries = [];
    notes.push(`кроме страны ${current.country}`);
  } else if (OTHER_PLACE.test(low)) {
    filters.noPlaces.push(place);
    notes.push(`кроме места ${current.city}`);
  }
  if (QUIET.test(low)) {
    filters.quiet = true;
    if (query.type === "excursion" || query.type === "active") query.type = "wellness";
    filters.noPlaces.push(place);
    notes.push("спокойное место, без ночной жизни и шопинга");
  }
  return { query, filters, notes };
}

function passes(dest, query, f, travelers) {
  const perPerson = perPersonCost(dest, query);
  return !((f.maxTotal !== null && perPerson * travelers > f.maxTotal)
    || (f.maxPerPerson !== null && perPerson > f.maxPerPerson)
    || (f.onlyCountries.length && !f.onlyCountries.includes(dest.country))
    || f.noCountries.includes(dest.country)
    || f.noContinents.includes(dest.continent)
    || f.noTypes.includes(dest.type)
    || f.noPlaces.includes(`${dest.country}|${dest.city}`)
    || (f.quiet && (dest.interest === "nightlife" || dest.interest === "shopping")));
}

// ---------------- Движок ----------------
// Имена критериев, какими их знает buildAbsurdNote() и правила ослабления.
const RELAXED_NAMES = {
  interest: "интересы и впечатления", duration: "продолжительность поездки", visa: "визовые предпочтения",
  season: "время года", budget: "бюджет", continent: "континент", type: "тип отдыха",
};
const CLOSE_GAP = 0.02; // варианты с такой разницей считаем равноценными
const pick = (list) => list[Math.floor(Math.random() * list.length)];
const percent = (v) => Math.floor(v * 100 + 0.5);
const placeOf = (d) => `${d.country}|${d.city}`;

function reasoning(dest, criteria, cost, value, total) {
  const place = `«${dest.country} — ${dest.city}»`;
  const parts = [value >= 0.75
    ? pick([
      `Сравнил направления базы (${total}) с вашими ответами: лучше всего подходит ${place}.`,
      `Из ${total} направлений ближе всего к вашему запросу ${place}.`,
      `Взвесил ваши ответы по каждому из ${total} направлений, и впереди оказалось место ${place}.`,
    ])
    : pick([
      `Точного попадания среди ${total} направлений нет. Ближе всего к запросу ${place}.`,
      `Запрос непростой: из ${total} направлений лучший компромисс сейчас ${place}.`,
    ])];
  const matched = criteria.filter((c) => c.status === "match").sort((a, b) => b.weight - a.weight);
  if (matched.length) parts.push("Совпадает: " + matched.map((c) => `${c.title.toLowerCase()} («${c.wanted}»)`).join(", ") + ".");
  for (const c of criteria) {
    if (c.status === "partial") parts.push(`Отчасти подходит ${c.title.toLowerCase()}: вы хотели «${c.wanted}», здесь «${c.actual}».`);
    else if (c.status === "mismatch") parts.push(`Пришлось уступить: ${c.title.toLowerCase()} здесь «${c.actual}», а не «${c.wanted}».`);
  }
  const tail = { within: ", это в рамках бюджета", over: ", это выше бюджета", below: ", это дешевле бюджета" };
  parts.push(`Примерная сумма ${spaced(cost.perPerson)} $ на человека${tail[cost.budgetStatus] ?? ""}.`);
  return parts.join(" ");
}

/** Советы по признакам направления из базы знаний. */
function tipsFor(dest, names) {
  const tips = [];
  if (dest.season !== "any") tips.push(`Лучшее время для поездки: ${names.season[dest.season].toLowerCase()}.`);
  tips.push({
    visa_free: "По нашей базе виза не нужна, но перед покупкой билетов проверьте правила для вашего гражданства.",
    easy_visa: "Виза оформляется по прибытии или онлайн: проверьте правила и срок действия загранпаспорта.",
    advance_visa: "Визу нужно оформить заранее: начните за 1–2 месяца до поездки.",
  }[dest.visa] ?? "Проверьте визовые правила для вашего гражданства на официальном сайте.");
  tips.push({
    beach: "Возьмите защиту от солнца и уточните сезон дождей и волн.",
    excursion: "Билеты в популярные места лучше купить заранее онлайн.",
    nature: "Пригодятся удобная обувь, дождевик и карта, доступная без интернета.",
    active: "Оформите страховку, которая покрывает активный отдых.",
    wellness: "Процедуры и места в спа бронируйте заранее.",
  }[dest.type] ?? "Заранее составьте список того, что хотите увидеть.");
  if (dest.interest === "family") tips.push("Место подходит для поездки с детьми: уточните в отеле детские условия.");
  else if (dest.budget === "economy" || dest.budget === "standard") tips.push("Билеты за 2–3 месяца до поездки обычно дешевле.");
  return tips;
}

function alternative(value, other, dest, query, travelers, names) {
  const mine = compareWithQuery(other, query, names), base = compareWithQuery(dest, query, names);
  const total = estimateCost(other, query, travelers).total, destTotal = estimateCost(dest, query, travelers).total;
  const better = mine.filter((c, i) => c.degree !== null && c.degree > base[i].degree).map((c) => c.title.toLowerCase());
  const worse = mine.filter((c, i) => c.degree !== null && c.degree < base[i].degree).map((c) => c.title.toLowerCase());
  const reason = [];
  if (better.length) reason.push("лучше: " + better.join(", "));
  if (worse.length) reason.push("хуже: " + worse.join(", "));
  if (total !== destTotal) reason.push(total < destTotal ? "дешевле" : "дороже");
  return { country: other.country, city: other.city, total, percent: percent(value), reason: reason.join("; ") || "почти так же хорошо" };
}

/** Лучшее направление или null, если уточнениям не отвечает ни одно. recent - места из истории. */
export function recommendSmart(destinations, query, travelers, names, recent = new Set(), filters = emptyFilters()) {
  const ranked = destinations.filter((d) => passes(d, query, filters, travelers))
    .map((dest) => ({ value: score(dest, query), dest })).sort((a, b) => b.value - a.value);
  // Одно место может встречаться в базе несколько раз - оставляем лучшую запись.
  const seen = new Set();
  const unique = ranked.filter(({ dest }) => !seen.has(placeOf(dest)) && seen.add(placeOf(dest)));
  if (!unique.length) return null;

  // Равноценные варианты не предлагаем по кругу: сначала те, которых не было в истории.
  const equal = unique.filter((u) => unique[0].value - u.value <= CLOSE_GAP);
  let fresh = equal.filter((u) => !recent.has(placeOf(u.dest)));
  if (!fresh.length) fresh = equal;
  const chosen = pick(fresh);
  const { dest, value } = chosen;
  const others = unique.filter((u) => u !== chosen);

  // Запасные варианты - из разных стран, чтобы выбор был настоящим.
  const countries = new Set([dest.country]);
  const diverse = others.filter((u) => !countries.has(u.dest.country) && countries.add(u.dest.country)).slice(0, 2);

  const criteria = compareWithQuery(dest, query, names);
  const cost = estimateCost(dest, query, travelers);
  return {
    country: dest.country, city: dest.city, description: dest.description, destination: dest,
    source: "ai", mode: "🤖 Наш ИИ-подбор", criteria, cost,
    relaxed: criteria.filter((c) => c.status === "partial" || c.status === "mismatch").map((c) => RELAXED_NAMES[c.key]),
    confidence: percent(value),
    reasoning: reasoning(dest, criteria, cost, value, unique.length),
    tips: tipsFor(dest, names),
    alternatives: diverse.map((u) => alternative(u.value, u.dest, dest, query, travelers, names)),
    considered: unique.length,
    totalDestinations: new Set(destinations.map(placeOf)).size,
    strong: unique.filter((u) => u.value >= 0.8).length,
    equal: equal.length,
    skippedRecent: equal.length - fresh.length,
    ranking: [chosen, ...others.slice(0, 4)].map((u) => ({
      place: `${u.dest.country} — ${u.dest.city}`, percent: percent(u.value), chosen: u === chosen,
    })),
    refinements: [],
  };
}
