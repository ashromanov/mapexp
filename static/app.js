import {
  kinds,
  severity,
  locationSummary as summarizeLocation,
  locationConfidence,
  locationNotice,
  uncertaintyRadius,
  colors,
  mapped,
  isStreamFresh,
  countLabel,
  filterEvents,
  safeTelegram,
  safeMedia,
  escapeHTML as esc,
} from "./ui.mjs?v=recall2";
// Resolve stable UI nodes once; dynamic detail nodes are deliberately not cached.
const ui = new Map([...document.querySelectorAll('[id]')].map((el) => ['#' + el.id, el]));
const $ = (s) => ui.get(s) || document.querySelector(s);
const cardTemplate = $('#event-card');
const cards = new Map();
const markerEvents = new Map();
const iconCache = new Map();
const locationLabels = new WeakMap();
function locationSummary(event) {
  let label = locationLabels.get(event);
  if (label === undefined) {
    label = summarizeLocation(event);
    locationLabels.set(event, label);
  }
  return label;
}
let renderFrame = 0;
function scheduleFeed() {
  if (!renderFrame) renderFrame = requestAnimationFrame(renderFeed);
}
function text(node, value) {
  if (node.textContent !== value) node.textContent = value;
}
const paths = {
  map: "M3 5l6-2 6 2 6-2v16l-6 2-6-2-6 2z M9 3v16 M15 5v16",
  search: "M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  help: "M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4 M12 17h.01 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  sliders: "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
  filter: "M3 6h18 M6 12h12 M9 18h6",
  plus: "M12 5v14 M5 12h14",
  minus: "M5 12h14",
  fit: "M8 3H3v5 M16 3h5v5 M21 16v5h-5 M8 21H3v-5",
  locate:
    "M12 2v3 M12 19v3 M2 12h3 M19 12h3 M19 12a7 7 0 1 1-14 0 7 7 0 0 1 14 0 M14 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0",
  expand: "M6 14l6-6 6 6",
  close: "M6 6l12 12 M18 6L6 18",
  back: "M20 12H4 M10 6l-6 6 6 6",
  pin: "M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0 M15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  fire: "M12 3c2 5 7 6 7 12a7 7 0 1 1-14 0c0-3 2-5 3-6 0 3 2 4 3 4 3-3 2-7 1-10",
  traffic:
    "M5 17H3v-5l3-7h12l3 7v5h-2 M5 17v3 M19 17v3 M3 12h18 M7 16h.01 M17 16h.01",
  utilities: "M12 2S4 11 4 15a8 8 0 0 0 16 0c0-4-8-13-8-13 M8 15c0 3 2 4 4 4",
  weather:
    "M4 16a5 5 0 1 1 2-9 7 7 0 1 1 13 6 M7 19l-1 3 M12 19l-1 3 M17 19l-1 3",
  crime: "M12 2l9 4v6c0 6-9 10-9 10S3 18 3 12V6z",
  health: "M9 3h6v6h6v6h-6v6H9v-6H3V9h6z",
  searchPeople:
    "M15 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M3 20v-2a7 7 0 0 1 10-6 M22 22l-3-3 M21 16a5 5 0 1 1-10 0 5 5 0 0 1 10 0",
  other: "M12 8v5 M12 17h.01 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  file: "M14 2H5v20h14V7z M14 2v6h5",
  refresh: "M20 7v5h-5 M20 12a8 8 0 1 0-2 6",
};
function icon(name) {
  if (iconCache.has(name)) return iconCache.get(name);
  const markup = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name === "search" ? "searchPeople" : name] || paths.other}"/></svg>`;
  iconCache.set(name, markup);
  return markup;
}
function hydrate(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    const name = el.dataset.icon;
    el.insertAdjacentHTML(
      "afterbegin",
      name === "search" ? iconSearch() : icon(name),
    );
    el.removeAttribute("data-icon");
  });
}
function iconSearch() {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="${paths.search}"/></svg>`;
}
hydrate();
let events = [],
  pendingEvents = null,
  config = {
    timezone: "Asia/Yekaterinburg",
    latitude: 58.01,
    longitude: 56.25,
    zoom: 11,
  },
  map,
  markers = new Map(),
  areas = new Map(),
  activeId = null,
  detailSequence = 0,
  refreshSequence = 0,
  streamReceivedAt = 0,
  pollHealthy = false,
  feedScroll = 0,
  loaded = false,
  feedETag = "";
const filters = { query: "", kind: "all", level: "all", location: "all" };
let dateFormatter, dateTimezone;
function date(value) {
  const d = new Date(value);
  return Number.isFinite(d.getTime())
    ? formatDate(d)
    : "Время не указано";
}
function formatDate(value) {
  const timezone = config.timezone || "Asia/Yekaterinburg";
  if (timezone !== dateTimezone) {
    dateTimezone = timezone;
    dateFormatter = new Intl.DateTimeFormat("ru", {
      timeZone: timezone, day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit",
    });
  }
  return dateFormatter.format(value);
}
function relative(value) {
  const n = Math.max(
    0,
    Math.floor((Date.now() - new Date(value).getTime()) / 60000),
  );
  return Number.isFinite(n)
    ? n < 1
      ? "только что"
      : n < 60
        ? `${n} мин назад`
        : date(value)
    : "Время не указано";
}
function badge(e) {
  return `<span class="badge ${esc(e.severity)}">${esc(severity[e.severity] || "Информация")}</span>`;
}
function locationMarkup(e) {
  return `<div class="location">${icon("pin")}<span>${esc(e.location_label || "Место не уточнено")}</span><span class="precision">${esc(locationSummary(e))}</span></div>`;
}
function state(title, text, action, label = "Повторить загрузку") {
  return `<div class="state">${icon("other")}<h2>${esc(title)}</h2><p>${esc(text)}</p>${action ? `<button class="button primary" id="state-action">${esc(label)}</button>` : ""}</div>`;
}
function updateCard(e) {
  let card = cards.get(e.id);
  if (!card) {
    const node = cardTemplate.content.firstElementChild.cloneNode(true);
    card = {
      node, kind: node.querySelector('.kind'), badge: node.querySelector('.badge'),
      time: node.querySelector('time'), title: node.querySelector('h2'),
      description: node.querySelector('p'), location: node.querySelector('.location-label'),
      precision: node.querySelector('.precision'), counts: node.querySelector('.card-counts'),
      sources: node.querySelector('.card-sources'), event: null,
    };
    node.dataset.event = e.id;
    cards.set(e.id, card);
  }
  const prior = card.event;
  if (!prior || prior.kind !== e.kind) {
    card.kind.innerHTML = icon(e.kind) + esc(kinds[e.kind] || "Другое");
  }
  if (!prior || prior.severity !== e.severity) {
    card.kind.className = `kind ${e.severity}`;
    card.badge.className = `badge ${e.severity}`;
    text(card.badge, severity[e.severity] || "Информация");
  }
  card.node.classList.toggle('selected', activeId === e.id);
  if (!prior || prior.last_confirmed_at !== e.last_confirmed_at)
    card.time.dateTime = e.last_confirmed_at;
  text(card.time, relative(e.last_confirmed_at));
  text(card.title, e.title);
  text(card.description, e.description);
  text(card.location, e.location_label || "Место не уточнено");
  text(card.precision, locationSummary(e));
  text(card.counts, `${countLabel(Number(e.updates) || 1, "сообщение", "сообщения", "сообщений")} · ${countLabel(Number(e.source_count) || 1, "источник", "источника", "источников")}`);
  text(card.sources, (e.source_names || []).join(', '));
  card.event = e;
  return card.node;
}
function renderFeed() {
  cancelAnimationFrame(renderFrame);
  renderFrame = 0;
  const visible = filterEvents(events, filters);
  text($("#count"), `${countLabel(visible.length, "событие", "события", "событий")} · ${visible.filter(mapped).length} на карте`);
  text($("#filter-summary"), `${filters.level === "all" ? "Все уровни" : severity[filters.level]} · ${filters.location === "all" ? "Все места" : filters.location === "mapped" ? "На карте" : "Без точки"}`);
  const feed = $("#feed");
  const scroll = feed.scrollTop;
  const live = new Set(events.map((e) => e.id));
  for (const [id, card] of cards) {
    if (!live.has(id)) {
      card.node.remove();
      cards.delete(id);
    }
  }
  if (visible.length) {
    const shown = new Set(visible.map((e) => e.id));
    for (const node of [...feed.children]) {
      if (!shown.has(node.dataset.event)) node.remove();
    }
    // Retain unchanged nodes in place, including focus and measured intrinsic sizes.
    let cursor = feed.firstElementChild;
    for (const e of visible) {
      const node = updateCard(e);
      if (node === cursor) cursor = cursor.nextElementSibling;
      else feed.insertBefore(node, cursor);
    }
  } else {
    const markup = state(
      events.length ? "Ничего не найдено" : "Пока нет активных событий",
      events.length ? "Попробуйте другой запрос или сбросьте фильтры." : "Новые сообщения появятся здесь после обработки источников.",
      events.length, "Сбросить фильтры",
    );
    if (feed.innerHTML !== markup) feed.innerHTML = markup;
    $("#state-action")?.addEventListener("click", resetFilters, { once: true });
  }
  feed.scrollTop = scroll;
  renderMarkers(visible);
}
function renderMarkers(visible) {
  if (!map) return;
  const shown = new Set();
  for (const e of visible) {
    if (!mapped(e)) continue;
    shown.add(e.id);
    const prior = markerEvents.get(e.id);
    let marker = markers.get(e.id);
    const moved = !prior || prior.latitude !== e.latitude || prior.longitude !== e.longitude;
    const title = `${e.title} — ${locationSummary(e)}`;
    if (!marker) {
      marker = L.marker([e.latitude, e.longitude], {
        title, keyboard: true, icon: markerIcon(e),
      }).addTo(map).on("click", () => openDetail(e.id));
      markers.set(e.id, marker);
    } else {
      if (moved) marker.setLatLng([e.latitude, e.longitude]);
      if (prior.kind !== e.kind || prior.severity !== e.severity) marker.setIcon(markerIcon(e));
      if (marker.options.title !== title) {
        marker.options.title = title;
        marker.getElement().title = title;
      }
    }
    const radius = uncertaintyRadius(e);
    let area = areas.get(e.id);
    if (!radius) {
      if (area) { area.remove(); areas.delete(e.id); }
    } else if (!area) {
      area = L.circle([e.latitude, e.longitude], {
        radius, color: colors[e.severity] || colors.info,
        opacity: e.id === activeId ? 0.6 : 0.3,
        fillOpacity: e.id === activeId ? 0.12 : 0.05,
        weight: e.id === activeId ? 2 : 1,
        dashArray: e.location_precision === "exact" ? null : "4 4",
        interactive: false,
      }).addTo(map);
      areas.set(e.id, area);
    } else {
      if (moved) area.setLatLng([e.latitude, e.longitude]);
      if (radius !== uncertaintyRadius(prior)) area.setRadius(radius);
      if (prior.severity !== e.severity || prior.location_precision !== e.location_precision)
        area.setStyle({ color: colors[e.severity] || colors.info, dashArray: e.location_precision === "exact" ? null : "4 4" });
    }
    markerEvents.set(e.id, e);
  }
  for (const [id, marker] of markers) {
    if (!shown.has(id)) {
      marker.remove(); markers.delete(id); markerEvents.delete(id);
      areas.get(id)?.remove(); areas.delete(id);
    }
  }
}
function markerIcon(e) {
  return L.divIcon({
    className: "",
    html: `<div class="event-marker ${esc(e.severity)} ${activeId === e.id ? "selected" : ""}">${icon(e.kind)}</div>`,
    iconSize: [30, 30], iconAnchor: [15, 15],
  });
}
let selectedId = null;
function selectCircle() {
  if (selectedId === activeId) return;
  for (const id of [selectedId, activeId]) {
    const selected = id === activeId;
    areas.get(id)?.setStyle({
      opacity: selected ? 0.6 : 0.3, fillOpacity: selected ? 0.12 : 0.05,
      weight: selected ? 2 : 1,
    });
    markers.get(id)?.getElement()?.firstElementChild.classList.toggle('selected', selected);
    cards.get(id)?.node.classList.toggle('selected', selected);
  }
  selectedId = activeId;
}
function mapUnavailable() {
  $("#map-error").hidden = false;
  $("#map-controls")?.setAttribute("aria-disabled", "true");
  document
    .querySelectorAll(".map-controls button")
    .forEach((b) => (b.disabled = true));
}
function initMap() {
  if (!window.L) {
    mapUnavailable();
    return;
  }
  try {
    if (map) map.remove();
    markers.clear();
    areas.clear();
    markerEvents.clear();
    map = L.map("map", { zoomControl: false, preferCanvas: true }).setView(
      [config.latitude, config.longitude],
      config.zoom,
    );
    let success = 0,
      errors = 0;
    const tiles = L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        maxZoom: 19,
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      },
    ).addTo(map);
    map.attributionControl.setPrefix(false);
    tiles.on("tileload", () => {
      success++;
      $("#map-error").hidden = true;
      document
        .querySelectorAll(".map-controls button")
        .forEach((b) => (b.disabled = false));
    });
    tiles.on("tileerror", () => {
      errors++;
      if (!success && errors >= 3) mapUnavailable();
    });
    setTimeout(() => {
      if (!success) mapUnavailable();
    }, 12000);
    $("#map-error").hidden = true;
    document
      .querySelectorAll(".map-controls button")
      .forEach((b) => (b.disabled = false));
    renderMarkers(filterEvents(events, filters));
  } catch {
    map = null;
    mapUnavailable();
  }
}
async function request(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(15000) });
  if (!response.ok) {
    const error = new Error("HTTP " + response.status);
    error.status = response.status;
    throw error;
  }
  return response.json();
}
async function refresh(initial = false) {
  const sequence = ++refreshSequence;
  try {
    const response = await fetch("/api/events", {
      signal: AbortSignal.timeout(15000),
      headers: feedETag && !initial ? { "If-None-Match": feedETag } : {},
    });
    if (response.status === 304) {
      if (sequence === refreshSequence && activeId) openDetail(activeId, false);
      return true;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    const items = await response.json();
    if (sequence !== refreshSequence) return true;
    if (!Array.isArray(items)) throw new Error("Invalid events");
    feedETag = response.headers.get("ETag") || "";
    if (initial || !loaded) {
      events = items;
      loaded = true;
      renderFeed();
    } else if (JSON.stringify(events) !== JSON.stringify(items)) {
      pendingEvents = items;
      $("#new-events").hidden = false;
      if (activeId) openDetail(activeId, false);
    }
    return true;
  } catch {
    if (sequence !== refreshSequence) return false;
    if (!loaded) {
      $("#feed").innerHTML = state(
        "Не удалось загрузить события",
        "Проверьте соединение и повторите попытку.",
        true,
      );
      $("#count").textContent = "Ошибка загрузки";
      $("#state-action").onclick = () => refresh(true);
    } else $("#stream-notice").hidden = false;
    return false;
  }
}
async function status() {
  try {
    const s = await request("/api/status");
    const last = s.last_polled_at || s.last_success_at;
    const prefix = s.provider_blocked
      ? "Обработка: провайдер недоступен"
      : !s.connected
        ? "Источники: нет связи"
        : "Источники: в сети";
    $("#freshness").textContent =
      `${prefix}${last ? ` · ${date(last)}` : " · Ожидаем обработку"}${s.pending ? ` · Очередь: ${s.pending}` : ""}`;
  } catch {
    $("#freshness").textContent = "Свежесть источников пока неизвестна";
  }
}
function mediaMarkup(media) {
  return (media || [])
    .map((m) => {
      const url = safeMedia(m.url);
      if (!url) return "";
      const mime = m.mime_type || "";
      if (mime.startsWith("image/") || m.kind === "photo")
        return `<img class="media" src="${esc(url)}" alt="Фото из сообщения источника" loading="lazy">`;
      if (mime.startsWith("video/") || m.kind === "video")
        return `<video class="media" src="${esc(url)}" controls preload="none" playsinline></video>`;
      if (mime.startsWith("audio/") || ["audio", "voice"].includes(m.kind))
        return `<audio class="media audio" src="${esc(url)}" controls preload="none"></audio>`;
      return `<a class="attachment button" href="${esc(url)}" download>${icon("file")}Скачать вложение</a>`;
    })
    .join("");
}
async function openDetail(id, focus = true) {
  const seq = ++detailSequence;
  if (!activeId) feedScroll = $("#feed").scrollTop;
  activeId = id;
  $("#feed").hidden = true;
  $("#detail").hidden = false;
  $("#feed-tools").hidden = true;
  $("#sidebar").classList.add("detail-open");
  if (focus && matchMedia("(max-width:1023px)").matches) setSheet(2);
  const scroll = focus ? 0 : $("#detail").scrollTop;
  if (focus)
    $("#detail").innerHTML =
      `<button class="button back" id="back">${icon("back")}К событиям</button><div class="state">Загружаем сообщения…</div>`;
  $("#back")?.addEventListener("click", closeDetail);
  selectCircle();
  try {
    const e = await request("/api/events/" + encodeURIComponent(id));
    if (seq !== detailSequence) return;
    const entries = e.feed || [];
    const placeNotice = locationNotice(e);
    $("#detail").innerHTML =
      `<button class="button back" id="back">${icon("back")}К событиям</button><div class="card-top"><span class="kind ${esc(e.severity)}">${icon(e.kind)}${esc(kinds[e.kind] || "Другое")}</span>${badge(e)}</div><h2>${esc(e.title)}</h2><p>${esc(e.description)}</p>${locationMarkup(e)}${placeNotice ? `<div class="inline-notice">${esc(placeNotice)}</div>` : ""}<div class="detail-meta">Обновлено ${esc(date(e.updated_at))} · ${countLabel(Number(e.source_count) || 1, "источник", "источника", "источников")}${e.location_source ? `<br>Место: ${esc(e.location_source)}` : ""}${locationConfidence(e) ? `<br>${esc(locationConfidence(e))} (оценка модели)` : ""}</div>${mapped(e) ? '<button class="button" id="show-on-map">Показать на карте</button>' : ""}<h3>Сообщения и обновления</h3>${
        entries.length
          ? entries
              .map((p) => {
                const link = safeTelegram(p.link);
                return `<article class="entry"><div class="entry-head"><strong>${esc(p.source_name || p.username || "Источник")}</strong><time>${esc(date(p.posted_at))}</time></div>${p.role === "comment" ? '<span class="precision">Комментарий</span>' : ""}<p class="source-text">${esc(p.text || "Сообщение содержит вложение")}</p>${p.edited_at ? `<p class="detail-meta">Изменено ${esc(date(p.edited_at))}</p>` : ""}${mediaMarkup(p.media)}${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Открыть в Telegram</a>` : ""}${p.revisions?.length ? `<details><summary>История изменений (${p.revisions.length})</summary>${p.revisions.map((v) => `<p class="source-text">${esc(v.text)}</p><p class="detail-meta">Версия ${Number(v.revision) || 1} · ${esc(date(v.recorded_at))}</p>`).join("")}</details>` : ""}</article>`;
              })
              .join("")
          : '<p class="detail-meta">Сообщения источников пока недоступны.</p>'
      }`;
    $("#detail").scrollTop = scroll;
    $("#back").onclick = closeDetail;
    $("#show-on-map")?.addEventListener("click", () => {
      if (map) {
        const fit = () => {
          const area = areas.get(e.id);
          if (area && e.location_precision !== "exact")
            map.fitBounds(area.getBounds(), { padding: [35, 35], maxZoom: 15 });
          else
            map.setView([e.latitude, e.longitude], Math.max(map.getZoom(), 13));
        };
        if (matchMedia("(max-width:1023px)").matches) setSheet(0, fit);
        else fit();
      } else notice("Карта пока недоступна.");
    });
    if (focus) $("#detail").focus({ preventScroll: true });
    if (mapped(e) && map && focus) map.panTo([e.latitude, e.longitude]);
    selectCircle();
  } catch (err) {
    if (seq !== detailSequence) return;
    $("#detail").innerHTML =
      `<button class="button back" id="back">${icon("back")}К событиям</button>${state(err.status === 404 ? "Событие недоступно" : "Не удалось загрузить событие", err.status === 404 ? "Возможно, срок его отображения закончился. Другие события доступны в ленте." : "Проверьте соединение и повторите попытку.", err.status !== 404)}`;
    $("#back").onclick = closeDetail;
    $("#state-action")?.addEventListener("click", () => openDetail(id));
  }
}
function closeDetail() {
  const old = activeId;
  activeId = null;
  detailSequence++;
  $("#detail").hidden = true;
  $("#feed").hidden = false;
  $("#feed-tools").hidden = false;
  $("#sidebar").classList.remove("detail-open");
  if (pendingEvents) applyUpdates();
  else renderFeed();
  $("#feed").scrollTop = feedScroll;
  selectCircle();
  const button = cards.get(old)?.node;
  (button?.isConnected ? button : $("#feed")).focus({ preventScroll: true });
}
function applyUpdates() {
  if (pendingEvents) {
    events = pendingEvents;
    pendingEvents = null;
  }
  $("#new-events").hidden = true;
  renderFeed();
  status();
}
function resetFilters() {
  Object.assign(filters, {
    query: "",
    kind: "all",
    level: "all",
    location: "all",
  });
  $("#search").value = "";
  document.querySelector("[name=severity][value=all]").checked = true;
  document.querySelector("[name=location][value=all]").checked = true;
  renderCategories();
  renderFeed();
}
function renderCategories() {
  $("#categories").innerHTML = [["all", "Все"], ...Object.entries(kinds)]
    .map(
      ([key, name]) =>
        `<button class="chip ${filters.kind === key ? "active" : ""}" data-kind="${key}" aria-pressed="${filters.kind === key}">${esc(name)}</button>`,
    )
    .join("");
}
renderCategories();
$("#categories").onclick = (e) => {
  const b = e.target.closest("[data-kind]");
  if (b) {
    filters.kind = b.dataset.kind;
    renderCategories();
    renderFeed();
  }
};
$("#search").addEventListener("input", (e) => {
  filters.query = e.target.value;
  scheduleFeed();
});
$("#feed").onclick = (e) => {
  const b = e.target.closest("[data-event]");
  if (b) openDetail(b.dataset.event);
};
$("#new-events").onclick = applyUpdates;
function showDialog(id) {
  const dialog = $(id);
  const prior = document.activeElement;
  dialog.showModal();
  dialog.addEventListener(
    "close",
    () => prior?.focus({ preventScroll: true }),
    { once: true },
  );
}
$("#filter-button").onclick = $("#filter-shortcut").onclick = () =>
  showDialog("#filters");
$("#help-button").onclick = () => showDialog("#help");
$("#apply-filters").onclick = () => {
  filters.level = document.querySelector("[name=severity]:checked").value;
  filters.location = document.querySelector("[name=location]:checked").value;
  renderFeed();
};
$("#reset-filters").onclick = resetFilters;
$("#zoom-in").onclick = () => map?.zoomIn();
$("#zoom-out").onclick = () => map?.zoomOut();
$("#fit-map").onclick = () => {
  if (!map) return;
  const coords = filterEvents(events, filters)
    .filter(mapped)
    .map((e) => [e.latitude, e.longitude]);
  coords.length
    ? map.fitBounds(coords, { padding: [50, 50], maxZoom: 14 })
    : map.setView([config.latitude, config.longitude], config.zoom);
};
$("#map-retry").onclick = initMap;
function notice(text) {
  $("#map-notice").textContent = text;
  $("#map-notice").hidden = false;
  setTimeout(() => ($("#map-notice").hidden = true), 6500);
}
$("#locate").onclick = () => {
  if (!navigator.geolocation) {
    notice("Браузер не поддерживает определение местоположения.");
    return;
  }
  notice("Определяем ваше местоположение…");
  navigator.geolocation.getCurrentPosition(
    (p) => {
      if (!map) return;
      L.circleMarker([p.coords.latitude, p.coords.longitude], {
        radius: 7,
        color: "#fff",
        weight: 2,
        fillColor: "#2457a7",
        fillOpacity: 1,
      }).addTo(map);
      map.setView([p.coords.latitude, p.coords.longitude], 14);
      notice("Показано ваше местоположение.");
    },
    () =>
      notice(
        "Не удалось определить местоположение. Разрешите доступ в браузере.",
      ),
    { timeout: 12000, maximumAge: 60000 },
  );
};
let sheet = 1,
  startY = 0,
  dragged = false;
function setSheet(n, afterResize) {
  sheet = Math.max(0, Math.min(2, n));
  document.querySelector(".map-area").inert = sheet === 2;
  document.documentElement.style.setProperty(
    "--sheet-height",
    ["96px", "50dvh", "90dvh"][sheet],
  );
  $("#sidebar").classList.toggle("peek", sheet === 0);
  $("#sidebar").classList.toggle("full", sheet === 2);
  $("#sheet-toggle").setAttribute(
    "aria-label",
    sheet === 2 ? "Свернуть ленту событий" : "Развернуть ленту событий",
  );
  $("#expand-feed").setAttribute(
    "aria-label",
    sheet === 2 ? "Свернуть ленту" : "Развернуть ленту",
  );
  setTimeout(() => {
    map?.invalidateSize();
    afterResize?.();
  }, 250);
}
$("#sheet-toggle").onclick = () => {
  if (dragged) {
    dragged = false;
    return;
  }
  setSheet(sheet === 2 ? 0 : sheet + 1);
};
$("#expand-feed").onclick = () => setSheet(sheet === 2 ? 1 : 2);
$("#sheet-handle").addEventListener("pointerdown", (e) => {
  startY = e.clientY;
  (e.target.closest("button") || e.currentTarget).setPointerCapture(e.pointerId);
});
$("#sheet-handle").addEventListener("pointerup", (e) => {
  const delta = e.clientY - startY;
  if (Math.abs(delta) > 25) {
    dragged = true;
    setSheet(sheet + (delta < 0 ? 1 : -1));
    e.preventDefault();
  }
});
async function bootstrap() {
  $("#feed").innerHTML =
    '<div class="skeleton"><span></span><span></span><span></span></div>'.repeat(
      3,
    );
  try {
    config = { ...config, ...(await request("/api/config")) };
    $("#city").textContent = config.city_name;
    document.title = `MapExp — ${config.city_name}`;
  } catch {
    notice("Настройки карты пока недоступны. Используется вид Перми.");
  }
  initMap();
  await refresh(true);
  status();
  const stream = new EventSource("/api/stream");
  function streamOnline() {
    streamReceivedAt = Date.now();
    pollHealthy = false;
    $("#connection").textContent = "Обновления подключены";
    $("#connection").setAttribute("aria-label", "Обновления подключены");
    $("#connection").classList.add("online");
    $("#stream-notice").hidden = true;
  }
  stream.onopen = () => {
    status();
  };
  stream.onerror = () => {
    streamReceivedAt = 0;
    if (pollHealthy) return;
    $("#connection").textContent = "Связь прервана";
    $("#connection").setAttribute(
      "aria-label",
      "Поток обновлений недоступен; проверяем события каждые 15 секунд.",
    );
    $("#connection").classList.remove("online");
    $("#stream-notice").hidden = false;
  };
  stream.addEventListener("heartbeat", streamOnline);
  stream.addEventListener("events", () => {
    streamOnline();
    refresh();
    status();
  });
  setInterval(async () => {
    if (isStreamFresh(streamReceivedAt)) return;
    const updated = await refresh();
    pollHealthy = updated;
    $("#connection").textContent = updated
      ? "Проверка каждые 15 с"
      : "Связь прервана";
    $("#connection").setAttribute(
      "aria-label",
      updated
        ? "События обновляются каждые 15 секунд"
        : "Связь прервана. Показаны последние загруженные события.",
    );
    $("#connection").classList.remove("online");
    $("#stream-notice").textContent = updated
      ? "Поток обновлений недоступен. Проверяем события каждые 15 секунд."
      : "Связь прервана. Показаны последние загруженные события.";
    $("#stream-notice").hidden = updated;
    status();
  }, 15000);
  setInterval(status, 60000);
  setInterval(() => {
    if (!activeId && loaded)
      cards.forEach((card) => {
        if (card.node.isConnected) text(card.time, relative(card.time.dateTime));
      });
  }, 60000);
  const initial = new URLSearchParams(window.location.search).get("event");
  if (initial) openDetail(initial);
}
bootstrap();
