# Recall-oriented event mapping

The mapping policy prioritizes placing a plausible local event on the map.
Uncertainty is expressed through approximate geometry and explanations. This
changes geography only: factual-support, relevance, source-revision checks and
incident lifetimes remain in force. Scores are model outputs, not calibrated
probabilities or measured recall.

1. Extract a place query, place type, and an exact supporting source quote.
   Colloquial landmarks, districts, intersections and nearby-place clues are valid.
   The quote must occur in the source or accepted evidence of the same event.
2. Geocode within the configured city scope (90 km). Preserve structured city,
   street and house fields from Photon and Nominatim. Compare complete house
   identifiers, including letters, fractions, buildings and structures. A city
   alone does not produce an arbitrary city-centre marker.
3. Continue to another query variant/provider when the first result is partial
   (wrong/missing house number or unrelated place), instead of stopping at any
   nonempty response. Rank relevant results before deduplication/limiting. Cache
   version v8 separates the new structured results from older cached responses.
4. Require a contextual grounding score of at least 0.35 to avoid using a place
   belonging to a clearly unrelated episode. A quoted full-address match in the
   configured city takes precedence over the model's separate confidence veto.
   Multiple matching objects within 300 m are treated as one address cluster;
   distant matching addresses remain ambiguous. Different house suffixes and
   street types never count as exact matches.
5. Otherwise accept the model's selected option at score >= 0.35, or fall back to
   a place-name token match >= 0.60 when the model chose none/was indecisive.
   Intersections and multiple named districts may use a partial token match >= 0.45,
   explicitly described as an approximate found section.
   There is no separate minimum confidence cutoff. Grounding and source evidence
   remain required. The selected coordinates always come from the geocoder.
6. Low-confidence full-address matches and "near/next to/opposite" wording become
   approximate POI areas. A partial address remains an approximate street area.
   Plausible alternative positions enlarge the radius; alternatives over 3.5 km
   apart produce an area rather than vetoing the entire match. Geometry requiring
   over 50 km of uncertainty remains unsupported.
7. Save `audit.location_resolution` with policy `recall-v1`, method, reason and
   original model scores. Event details expose the reason for the currently
   stored coordinates, and the UI explains approximate areas. The score is not
   changed to manufacture confidence in a deterministic address match.

The motivating regression is `шоссе Космонавтов, 113А`: the source quote and one
address cluster match, but model confidence 0.58 previously failed a 0.60 veto.
It now maps to the geocoder coordinates with a 432 m approximate area. The 0.58
score is retained. This is an observed example, not a measured accuracy claim.

## Reprocessing existing data

```bash
# Preview one event using existing audits where possible:
.venv/bin/python remap.py --database data/mapexp.sqlite3 --event EVENT_ID
# Apply to existing unlocated events, optionally retrying missing place extraction:
.venv/bin/python remap.py --database data/mapexp.sqlite3 --extract-missing --apply
```

The tool creates an SQLite online backup before applying changes. Preview does
not update event fields, but may call external providers and populate the geocoder
cache. Existing audited candidates are reused when adequate. Fresh candidate lists
receive fresh contextual verification; old answer indices are not reused for a
new list. `--extract-missing` retries extraction using accepted source evidence,
while discarding the newly generated title/description.

Only geographic fields, revision and update timestamp change. Source membership,
accepted text, event prose, severity, first/last report times and expiry are
preserved. A compare-and-swap on the event revision prevents overwriting a
concurrent ingestion update. Repeated application of the same result is a no-op.
The geographic repair records a separate `remap` decision for auditability.

## Checks

```bash
.venv/bin/python selftest.py
node static/ui.test.mjs
```

Tests cover the motivating 0.58 case, model-none fallback, foreign context,
wrong house/corpus/city/street, clustered duplicates, distant alternatives,
nearby-place wording, weak landmark matches, partial-provider fallback, cache
reuse, unchanged prose/expiry/evidence, preview, idempotency and concurrent writes.
Live reprocessing measurements are recorded separately from synthetic tests;
no labeled precision/recall evaluation has been performed.
