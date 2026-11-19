# Data Provenance & Licences

Every JSON data file in this directory MUST carry a `license` and
`attribution` object before it merges — same rule as the asset sets
(`ogp-asset-forge`: no provenance, no merge). A unit test
(`tests/unit/test_data_file_licenses.py`) enforces this gate.

## Licence

All bundled data files are licensed under **CC BY-SA 4.0** (Creative Commons
Attribution-ShareAlike 4.0 International), consistent with the upstream
Permapeople data (the primary online source) and the share-alike principle.

Each file carries:

```json
{
  "license": {
    "spdx": "CC-BY-SA-4.0",
    "name": "Creative Commons Attribution-ShareAlike 4.0 International",
    "url": "https://creativecommons.org/licenses/by-sa/4.0/"
  },
  "attribution": {
    "sources": ["..."],
    "note": "Curated from public horticultural references. Derived data, not verbatim copies."
  }
}
```

## Files

| file | sources |
| ---- | ------- |
| `plant_species.json` | RHS, Cornell, Kew, USDA/GRIN, UMN, OSU; Arizona set: compiled knowledge (see below) |
| `companion_planting.json` | RHS, Riotte, Rodale |
| `seed_viability.json` | OSU, Johnny's Selected Seeds, Mother Earth News, RHS |
| `amendments.json` | Rodale, RHS, USDA Extension |

## Online data sources

When plant data is fetched at runtime from online APIs, the attribution is
shown in-app (Plant Details panel + About dialog):

| provider | licence | attribution required |
| -------- | ------- | -------------------- |
| Permapeople | CC BY-SA 4.0 | Yes — link-back to permapeople.org |
| Trefle | Per their API terms | Yes — attribution wording per Trefle docs |
| Perenual | Free personal/commercial | Link-back recommended |

## USDA sources (build-time import only)

`scripts/import_usda_species.py` can merge USDA taxonomy into `plant_species.json`
offline. The app never contacts USDA at run time, so `PRIVACY.md` and the dependency
set are unchanged. Raw downloads are cached outside the repository and never committed.
The script writes only the import-owned keys `usda_symbol`, `usda_duration`,
`usda_growth_habit`, `usda_native_status` and `usda_state_presence`, and fills an absent
`family`, `genus` or `cycle`. It never overwrites a hand-curated value.

| source | provides | does NOT provide | terms (as established 2026-11-19) |
| ------ | -------- | ---------------- | --------------------------------- |
| USDA NRCS PLANTS (plants.usda.gov), official download lists | accepted name and synonyms, family, duration, growth habit, native status per jurisdiction (L48, AK, HI, PR, VI, CAN), presence in a state via a state checklist | edibility, water, sun, pH, size, hardiness, calendar; native status per US state (only presence per state) | data.gov lists the dataset as CC0; USDA's "Conditions of Image and Data Use" calls plant information uncopyrighted and asks for a citation (USDA, NRCS. The PLANTS Database, http://plants.usda.gov). **Images are separate and mostly need permission: none are used.** |
| ARS GRIN taxonomy (ars-grin.gov, GRIN-Global) | accepted name, synonyms, family, genus, common names, economic-plant flags | per-plant hardiness or edibility fields in the bulk taxonomy export | data.gov lists GRIN-Global as US government public domain; the ARS site carries no separate data-use text; the GBIF copy of GRIN Taxonomy cites Wiersema (2019) |
| `plantsservices.sc.egov.usda.gov` REST API | what the plants.usda.gov web app calls | n/a | undocumented, no published terms: **not used** |

Caveats, stated plainly. On 2026-11-19 `plants.usda.gov`, `plants.sc.egov.usda.gov`
and `npgsweb.ars-grin.gov` answered 504/503, so the PLANTS terms PDF, the download page
and the GRIN bulk export were not read at the primary source: the terms above come from
the data.gov catalogue entries and search summaries of those documents, and the column
names the importer accepts come from archived download pages. Re-read both on first use
and adjust `_PLANTS_HEADERS`/`_GRIN_HEADERS` if a header differs (the importer stops with
the header list when a required column is missing). **No USDA value has been imported
into `plant_species.json` yet**, so no USDA field is bundled and no in-app credit is
due; add the About-dialog credit in the same change that first imports data.

## Arizona lower-desert set

The 41 records tagged `regions: ["arizona_lower_desert"]` (Mesa, USDA zone 9b/10a) are
hand-compiled from general horticultural knowledge, not copied from a table, and are not
verified against a cited reference. A value the author was unsure of is listed in the
record's `uncertain_fields`; `notes` carries safety and local-practice remarks. The schema
holds one window per task, so a second (fall) sowing of a spring crop is described in
`notes`, not encoded; crops sown in autumn (mustard greens) use a late-year window, in
weeks after the spring frost (about 10 Feb).

## Rule

**No data source without licence + in-app credit.** New data files or new
online providers must land with both a `license`/`attribution` block in the
JSON and an entry in the About dialog's "Data sources & licenses" section.
