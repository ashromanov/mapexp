import assert from "node:assert/strict";
import {
  filterEvents,
  safeTelegram,
  safeMedia,
  mapped,
  locationSummary,
  locationNotice,
  locationConfidence,
  uncertaintyRadius,
  isStreamFresh,
  countLabel,
  escapeHTML,
} from "./ui.mjs";
const events = [
  {
    kind: "fire",
    severity: "danger",
    title: "Дым на улице Ленина",
    latitude: 58,
    longitude: 56,
  },
  {
    kind: "other",
    severity: "info",
    title: "Громкий звук",
    latitude: null,
    longitude: null,
  },
];
assert.equal(
  filterEvents(events, { query: "дым Ленина", location: "mapped" }).length,
  1,
);
assert.equal(filterEvents(events, { location: "unknown" })[0], events[1]);
assert.equal(mapped({ latitude: 100, longitude: 56 }), false);
const area = {
  latitude: 58,
  longitude: 56,
  location_precision: "district",
  radius_m: 1200,
  location_confidence: 0.88,
};
assert.equal(mapped({ ...area, location_precision: "city" }), false);
assert.equal(mapped({ ...area, location_precision: "unknown" }), false);
assert.equal(locationSummary(area), "Район · радиус ~1,2 км");
assert.equal(
  locationSummary({ ...area, location_precision: "area" }),
  "Область · радиус ~1,2 км",
);
assert.equal(
  locationSummary({ ...area, location_precision: "city" }),
  "Без точки на карте",
);
assert.equal(locationConfidence(area), "Оценка привязки: средняя");
assert.equal(locationConfidence({ ...area, location_confidence: null }), "");
assert.equal(locationConfidence({ ...area, location_confidence: 1.1 }), "");
assert.equal(uncertaintyRadius({ ...area, radius_m: Infinity }), 0);
assert.equal(safeTelegram("https://t.me/channel/1"), "https://t.me/channel/1");
assert.equal(safeTelegram("https://t.me.evil.com/x"), null);
assert.equal(safeTelegram("javascript:alert(1)"), null);
assert.equal(safeMedia("/media/photo.jpg"), "/media/photo.jpg");
assert.equal(safeMedia("/media/../secret"), null);
assert.equal(safeMedia("//evil.com/media/a"), null);
assert.equal(escapeHTML("<img>"), "&lt;img&gt;");
assert.equal(isStreamFresh(0, 1000), false);
assert.equal(isStreamFresh(1000, 21000), false);
assert.equal(isStreamFresh(1000, 19000), true);
assert.equal(countLabel(2, "событие", "события", "событий"), "2 события");
assert.equal(countLabel(11, "событие", "события", "событий"), "11 событий");
assert.equal(
  countLabel(21, "сообщение", "сообщения", "сообщений"),
  "21 сообщение",
);
const knownAddress = {
  location_label: "шоссе Космонавтов, 113А",
  location_precision: "unknown", latitude: null, longitude: null,
};
assert.equal(mapped(knownAddress), false);
assert.equal(locationSummary(knownAddress), "Координаты не подтверждены");
assert.match(locationNotice(knownAddress), /Место указано в источнике/);
assert.doesNotMatch(locationNotice(knownAddress), /нет достаточно конкретного места/);
assert.equal(locationSummary({ ...knownAddress, location_label: null }), "Без точки на карте");
assert.match(locationNotice({ ...knownAddress, location_label: null }), /пока не установлено/);
assert.match(locationNotice({ ...knownAddress, location_precision: "city" }), /только город/);
assert.equal(locationNotice({ ...area, location_precision: "exact" }), "");
assert.match(locationNotice(area), /приблизительную область/);
console.log("UI logic checks passed");
