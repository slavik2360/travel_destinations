// Порт DestinationImageLoader из MyProgram/main.py: сначала статья, названная точно как
// место, затем "Место (город)" (частый вариант неоднозначности), и только в последнюю
// очередь - полнотекстовый поиск с отсевом людей и флагов/карт. origin=* включает CORS для
// анонимных запросов с произвольного сайта (GitHub Pages) - без него браузер заблокирует ответ.
const API = "https://ru.wikipedia.org/w/api.php";
const TIMEOUT_MS = 6000;

const NON_PHOTO_HINTS = [
  "flag_of", "flag-", "coat_of_arms", "coatofarms", "blank_map", "locator_map",
  "location_map", "_locator", "emblem_of", "seal_of", "_map_of", "map_of_", ".svg",
];
const PERSON_WORD_PREFIXES = [
  "актёр", "актрис", "певец", "певиц", "политик", "предпринимат", "писатель", "поэт",
  "режиссёр", "журналист", "спортсмен", "футболист", "хоккеист", "баскетболист",
  "теннисист", "боксёр", "боец", "музыкант", "художни", "учён", "инженер", "блогер",
  "телеведущ", "фотограф", "дизайнер", "продюсер", "композитор", "дирижёр", "историк",
  "архитектор", "философ", "экономист", "юрист", "врач", "профессор", "академик",
  "чемпион", "депутат", "министр", "президент", "монах", "епископ", "генерал",
  "адмирал", "офицер",
];
const PERSON_SUBSTRING_HINTS = ["модель (", "(р. ", "(род. ", "родился", "родилась"];
const WORD_RE = /[a-zа-яё]+/gi;

function looksLikePhoto(url) {
  const lowered = url.toLowerCase();
  return !NON_PHOTO_HINTS.some((hint) => lowered.includes(hint));
}

function looksLikePerson(description) {
  if (!description) return false;
  const lowered = description.toLowerCase();
  if (PERSON_SUBSTRING_HINTS.some((hint) => lowered.includes(hint))) return true;
  const words = lowered.match(WORD_RE) || [];
  return words.some((w) => PERSON_WORD_PREFIXES.some((p) => w.startsWith(p)));
}

function looksLikePersonPage(page) {
  if ((page.title || "").includes(",")) return true;
  return looksLikePerson(page.description);
}

function cleanPlaceName(text) {
  return text.split("(")[0].split("+")[0].trim();
}

async function apiGet(params) {
  const url = new URL(API);
  url.search = new URLSearchParams({
    action: "query",
    format: "json",
    formatversion: "2",
    prop: "pageimages|description",
    piprop: "thumbnail",
    pithumbsize: "600",
    origin: "*",
    ...params,
  }).toString();

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function tryExactTitle(title) {
  const data = await apiGet({ titles: title, redirects: "1" });
  const page = data?.query?.pages?.[0];
  if (!page || page.missing) return null;
  const thumb = page.thumbnail?.source;
  if (thumb && looksLikePhoto(thumb) && !looksLikePersonPage(page)) return thumb;
  return null;
}

async function trySearch(searchQuery) {
  const data = await apiGet({ generator: "search", gsrsearch: searchQuery, gsrlimit: "5" });
  const pages = (data?.query?.pages || []).slice().sort((a, b) => (a.index ?? 999) - (b.index ?? 999));
  for (const page of pages) {
    const thumb = page.thumbnail?.source;
    if (!thumb || looksLikePersonPage(page)) continue;
    if (looksLikePhoto(thumb)) return thumb;
  }
  return null;
}

/** Возвращает URL фотографии места или null (вызывающий код в этом случае просто
 *  оставляет эмодзи-иконку - наличие фото никак не влияет на логику подбора). */
export async function fetchDestinationImageUrl(country, city) {
  const place = cleanPlaceName(city);
  return (
    (await tryExactTitle(place)) ||
    (await tryExactTitle(`${place} (город)`)) ||
    (await trySearch(`${place} ${country}`.trim()))
  );
}
