import DATA from "./data.js";
import { getCustomDestinations, getDeletedBuiltinIds } from "./storage.js";

function tupleToDest(tuple, id) {
  const [budget, continent, type, season, duration, visa, interest, country, city, description] = tuple;
  return { id, budget, continent, type, season, duration, visa, interest, country, city, description, builtin: true };
}

/** Встроенные 213 направлений + добавленные пользователем вручную, минус удалённые. */
export function loadDestinations() {
  const deleted = getDeletedBuiltinIds();
  const builtin = DATA.DESTINATIONS.map((t, i) => tupleToDest(t, `builtin-${i}`)).filter(
    (d) => !deleted.has(d.id)
  );
  return builtin.concat(getCustomDestinations());
}

export function makeDestinationId() {
  return `custom-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
