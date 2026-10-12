// Веб-версия работает без сервера и без SQLite - история подборов и направления,
// добавленные вручную через "Базу знаний", хранятся в localStorage браузера (на устройстве
// пользователя). Это соответствует природе статического сайта на GitHub Pages: нет бэкенда,
// которому можно было бы писать в общий файл БД.
const HISTORY_KEY = "travel_expert_history_v1";
const CUSTOM_DEST_KEY = "travel_expert_custom_destinations_v1";
const DELETED_BUILTIN_KEY = "travel_expert_deleted_builtin_v1";

function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}
function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // localStorage недоступен (приватный режим и т.п.) - молча не сохраняем,
    // приложение продолжает работать, просто без сохранения истории/правок.
  }
}

export function getHistory() {
  return readJSON(HISTORY_KEY, []);
}
export function addHistoryEntry(entry) {
  const list = getHistory();
  list.unshift(entry);
  writeJSON(HISTORY_KEY, list);
}
export function clearHistory() {
  writeJSON(HISTORY_KEY, []);
}

export function getCustomDestinations() {
  return readJSON(CUSTOM_DEST_KEY, []);
}
export function addCustomDestination(dest) {
  const list = getCustomDestinations();
  list.push(dest);
  writeJSON(CUSTOM_DEST_KEY, list);
}
export function removeCustomDestination(id) {
  writeJSON(CUSTOM_DEST_KEY, getCustomDestinations().filter((d) => d.id !== id));
}

export function getDeletedBuiltinIds() {
  return new Set(readJSON(DELETED_BUILTIN_KEY, []));
}
export function deleteBuiltinDestination(id) {
  const set = getDeletedBuiltinIds();
  set.add(id);
  writeJSON(DELETED_BUILTIN_KEY, Array.from(set));
}
