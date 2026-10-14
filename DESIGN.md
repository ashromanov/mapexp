---
name: Civic Incident Pulse
colors:
  primary: '#0f5e66'
  primary-container: '#e9f3f3'
  secondary: '#2457a7'
  secondary-container: '#eaf1fc'
  tertiary: '#8a5700'
  tertiary-container: '#fff3d6'
  error: '#b42332'
  error-container: '#fcecef'
  critical: '#7d1730'
  critical-container: '#f8e6ec'
  outline: '#bfc8ca'
  selected-outline: '#8fb5b7'
  primary-dark: '#00454c'
  tertiary-dark: '#653e00'
  connection-success: '#197347'
  map-substrate: '#e4eced'
  loading: '#e9edf0'
  selection: '#abeef7'
  on-selection: '#001f23'
  background: '#f4f6f8'
  surface: '#ffffff'
  surface-container-low: '#e8f5ff'
  on-surface: '#17232b'
  on-surface-variant: '#53636d'
  outline-variant: '#dce3e7'
typography:
  headline-lg:
    fontFamily: Manrope
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 33px
  headline-sm:
    fontFamily: Manrope
    fontSize: 17px
    fontWeight: '700'
    lineHeight: 26px
  headline-md:
    fontFamily: Manrope
    fontSize: 22px
    fontWeight: '700'
    lineHeight: 31px
  section-title:
    fontFamily: Manrope
    fontSize: 20px
    fontWeight: '700'
    lineHeight: 30px
  body-sm:
    fontFamily: Manrope
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 22px
  caption:
    fontFamily: Manrope
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 19px
  body-md:
    fontFamily: Manrope
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 24px
  label-sm:
    fontFamily: Manrope
    fontSize: 13px
    fontWeight: '600'
    lineHeight: 20px
rounded:
  xs: 5px
  source: 8px
  sm: 10px
  floating: 12px
  dialog: 16px
  md: 14px
  lg: 20px
  full: 9999px
spacing:
  space-xs: 4px
  space-sm: 8px
  space-md: 12px
  space-lg: 16px
  space-xl: 20px
  space-2xl: 24px
---

## Overview

**Creative North Star: "Civic Incident Pulse"**

The supplied Stitch screens define a calm public service for reading city disruptions in daylight. White controls, a pale blue feed, teal selected states, and legible Manrope type keep evidence visible without making danger theatrical. The map is actual cartography; the interface never paints fictional streets or incidents.

**Key Characteristics:**
- A clear map and feed relationship.
- Severity conveyed by text and color together.
- Original source messages remain readable.
- Compact, daylight-friendly controls.

## Colors

Teal owns product identity and primary actions. Blue, amber, red, and deep burgundy distinguish information, warning, danger, and critical reports. Each severity pairs dark text with a pale tint. Slate metadata remains legible against white and pale blue.

**The Evidence Rule.** Connection status reflects received stream events or successful 15-second polling; source timestamps and source-worker freshness remain separate evidence.

## Typography

Manrope is self-hosted in regular, semibold, and bold weights. Incident titles use clear weight and scale rather than decorative display lettering. Source messages wrap long untrusted text. Times use tabular numerals. Functional metadata stays at least 12px at every viewport; page headings stay at least 22px and detail titles use 24px.

## Layout

Cartographic tiles are modestly desaturated while markers retain semantic colors. The mobile half-sheet hides the duplicate filter row (filters remain in the search control) and legend so an incident preview is visible. The full sheet restores the filter row. City context remains in the header. The map resizes to its visible area above the mobile sheet, retaining a 20px overlap beneath the rounded corners. The full sheet makes the map inert and hides its controls. POIs use the “Ориентир” precision label, never “Точный адрес”. Approximate locations show a faint dashed uncertainty circle even before selection; selection emphasizes the area. Cards name the radius, details show the qualitative model estimate where available alongside the original source messages. The marker is the area center, not a claimed exact incident address. City-only and unknown locations stay off the map, including legacy coordinates.

Desktop uses an 80px product header and a 460px sidebar next to the map. Below 1024px, a 64px header sits above the map and a bottom sheet snaps to 96px, 50dvh, or 90dvh. The sheet contains a drag handle and equivalent labeled buttons. Detail replaces the feed while preserving its scroll position.

## Elevation & Depth

Docked areas use hairline structural dividers. Floating map controls use a soft low shadow; the mobile sheet, dialogs, and legend use a wider ambient shadow. The cartography remains unobstructed wherever controls do not need to occupy it.

## Shapes

Controls have gently curved corners, incident cards use medium rounded corners, and mobile sheets round only the upper edge. Chips and severity badges are small capsules. Map incidents use circular markers with an additional outline for selection.

## Components

Search filters incident text, places, and source names. Eight category chips remain available by horizontal scrolling. Filters constrain severity and coordinate availability. Cards expose title, description, location precision, source count, and message count. Details expose the original timeline and documentary media without autoplay. Accessible dialogs contain filters and explanatory help. Loading, empty results, failed transport, failed initial load, unavailable map, and expired event each explain recovery. Healthy polling displays “Проверка каждые 15 с” without a warning; an unavailable stream alone does not make successful polling unhealthy.

**The Reading Position Rule.** New data waits behind a visible update action instead of moving the feed under the reader; an open detail refreshes at its current scroll offset.

## Do's and Don'ts

### Do:
- **Do** retain the supplied light civic identity.
- **Do** show uncertainty through labels and a map radius.
- **Do** preserve the feed when cartography fails.
- **Do** use original documentary media and source links.

### Don't:
- **Don't** mix demonstration incidents into live reports.
- **Don't** claim that stream connectivity proves source freshness.
- **Don't** autoplay source media.
