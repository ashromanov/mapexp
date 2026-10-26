export const kinds = {
  traffic: "Дороги",
  fire: "Пожары",
  utilities: "ЖКХ",
  weather: "Погода",
  crime: "Безопасность",
  health: "Здоровье",
  search: "Поиск людей",
  other: "Другое",
};
export const severity = {
  critical: "Высокая опасность",
  danger: "Опасность",
  warning: "Предупреждение",
  info: "Информация",
};
export const precision = {
  exact: "Точный адрес",
  poi: "Ориентир",
  street: "Указана улица",
  district: "Район",
  area: "Область",
  city: "Город",
  unknown: "Без точки на карте",
};
export const colors = {
  critical: "#7d1730",
  danger: "#b42332",
  warning: "#8a5700",
  info: "#2457a7",
};
export const mapped = (e) =>
  !["city", "unknown"].includes(e.location_precision) &&
  Number.isFinite(e.latitude) &&
  Number.isFinite(e.longitude) &&
  Math.abs(e.latitude) <= 90 &&
  Math.abs(e.longitude) <= 180;
export function uncertaintyRadius(e) {
  return mapped(e) && Number.isFinite(e.radius_m) && e.radius_m > 0
    ? e.radius_m
    : 0;
}
function reportedPlace(e) {
  const label = String(e.location_label || "").trim();
  return e.location_precision !== "city" && label &&
    !/^(место не уточнено|место неизвестно|адрес не указан)$/i.test(label);
}
export function locationNotice(e) {
  if (mapped(e)) {
    if (e.location_precision === "exact") return "";
    const reason = e.location_reason ? e.location_reason + ". " : "";
    return reason + "Пунктирный круг показывает приблизительную область, а маркер — её центр. Место события внутри круга не установлено точно.";
  }
  if (e.location_precision === "city")
    return "В источнике указан только город. Более точное место пока не установлено; событие доступно без точки на карте.";
  if (reportedPlace(e)) {
    const reason = e.location_reason ? e.location_reason + ". " : "";
    return reason + "Место указано в источнике, но его координаты пока не подтверждены. Адрес или ориентир сохранён в карточке; точки на карте пока нет.";
  }
  return "Место события пока не установлено. Событие доступно в ленте без точки на карте.";
}
export function locationSummary(e) {
  if (!mapped(e)) return reportedPlace(e) ? "Координаты не подтверждены" : "Без точки на карте";
  const label = precision[e.location_precision] || "Место приблизительно";
  const radius = uncertaintyRadius(e);
  if (!radius || e.location_precision === "exact") return label;
  const distance =
    radius >= 1000
      ? `${new Intl.NumberFormat("ru", { maximumFractionDigits: 1 }).format(radius / 1000)} км`
      : `${Math.round(radius)} м`;
  return `${label} · радиус ~${distance}`;
}
export function locationConfidence(e) {
  const value = e.location_confidence;
  if (!mapped(e) || !Number.isFinite(value) || value < 0 || value > 1)
    return "";
  return `Оценка привязки: ${value >= 0.9 ? "высокая" : value >= 0.65 ? "средняя" : "низкая"}`;
}
export function filterEvents(
  events,
  { query = "", kind = "all", level = "all", location = "all" },
) {
  const words = query
    .toLocaleLowerCase("ru")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  return events.filter(
    (e) =>
      (kind === "all" || e.kind === kind) &&
      (level === "all" || e.severity === level) &&
      (location === "all" ||
        (location === "mapped" ? mapped(e) : !mapped(e))) &&
      words.every((w) =>
        [e.title, e.description, e.location_label, ...(e.source_names || [])]
          .join(" ")
          .toLocaleLowerCase("ru")
          .includes(w),
      ),
  );
}
export function safeTelegram(value) {
  try {
    const u = new URL(value);
    return u.protocol === "https:" &&
      u.hostname === "t.me" &&
      !u.username &&
      !u.password
      ? u.href
      : null;
  } catch {
    return null;
  }
}
export function safeMedia(value) {
  if (
    typeof value !== "string" ||
    !value.startsWith("/media/") ||
    value.startsWith("//") ||
    value.includes("\\")
  )
    return null;
  try {
    const u = new URL(value, "https://mapexp.invalid");
    return u.origin === "https://mapexp.invalid" &&
      u.pathname.startsWith("/media/")
      ? u.pathname + u.search
      : null;
  } catch {
    return null;
  }
}
export function escapeHTML(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}

export function isStreamFresh(lastReceivedAt, now = Date.now()) {
  return lastReceivedAt > 0 && now - lastReceivedAt < 20000;
}

const countRules = new Intl.PluralRules("ru");
export function countLabel(count, one, few, many) {
  return `${count} ${{ one, few, many, other: many }[countRules.select(count)]}`;
}
