# MapExp

<!-- impeccable:product-schema 1 -->

## Platform
web

## Stack
FastAPI, SQLite WAL, Telethon, Leaflet, modular browser JavaScript and CSS. No frontend build system.

## Users
Residents of Perm checking nearby disruptions on a phone or desktop.

## Product Purpose
A live public incident map and readable feed assembled from the configured public Telegram channels. Readers can inspect original reports, updates, comments and media.

## Capabilities and Constraints
One city and surrounding area. Jev makes discrete relevance, matching, novelty and location decisions; the configured text model extracts information and writes neutral summaries. Plausible uncertain reports remain visible with attribution; unresolved locations stay off the map. Coordinates must come from scoped geocoders. Edits preserve evidence. Reposts cannot renew event lifetime. Existing data, source configuration and credentials are preserved.

## Brand Commitments
MapExp, Russian interface, supplied Stitch ZIP's light civic identity with functional and accessibility fixes.

## Evidence on Hand
stitch_mapexp_design_system.zip supplies desktop/mobile screens and a design system. Real local database and configured source channels exist. Demonstration content must be labeled and never mixed into the live feed.

## Product Principles
- Source evidence before generated claims.
- Honest uncertainty and honest connection status.
- Feed remains usable when the map fails.
- Preserve reading position during live updates.

## Accessibility & Inclusion
Keyboard operation, visible focus, accessible dialogs, touch targets and reduced-motion support.
