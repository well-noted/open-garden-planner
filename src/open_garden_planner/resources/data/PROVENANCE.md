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

Retrieved 2026-11-20 (owner-approved download, polite single requests, 2 s apart, cached
outside the repo, never committed):

- **PLANTS Complete Checklist**: `https://plants.sc.egov.usda.gov/DocumentLibrary/Txt/plantlst.txt`
  (`plants.usda.gov/home/downloads` redirects there; the Downloads page links it as
  "Download the Complete PLANTS Checklist"). 7,038,655 bytes, `Last-Modified` 2025-05-29,
  columns Symbol, Synonym Symbol, Scientific Name with Authors, Common Name, Family. **It
  carries no duration, growth habit or native status**, so the import fills only
  `usda_symbol` (149 of 159 species matched; the 10 unmatched are cultivar/hybrid names such
  as `Brassica oleracea var. capitata`, `Fragaria ananassa`, `Magnolia x soulangeana`).
  Family, genus and cycle were already present, so nothing else changed.
- **State checklist**: the Downloads page offers per-state lists only to "NRCS personnel and
  cooperators", and `DocumentLibrary/Txt/AZ_NRCS_csv.txt` is not served publicly (the host
  returns the app shell). **Not imported**, so `usda_state_presence` stays unset.
- **Duration, growth habit, native status** exist only through the site's Search Results
  CSV, which the web app builds from the undocumented `plantsservices` REST API listed above
  as not used. **Not imported**: needs the owner's decision to use that API.
- **ARS GRIN**: `npgsweb.ars-grin.gov/gringlobal/taxon/taxonomysearch` answers 200 but the
  taxonomy pages link no bulk export file (the obvious `taxonomydownload` URL is 404).
  **Not imported.**

Terms read at the primary source on 2026-11-20: the PLANTS Downloads and Usage pages state
no terms for the data files; the Usage page's "not copyrighted" sentence is about images,
and the live site no longer serves the older Citations/Help PDFs. The data licence therefore
still rests on the data.gov CC0 entry and the earlier "uncopyrighted, please cite" wording
above. The GRIN site states no data-use terms at all. Credit to add with the first real data
import: USDA, NRCS. The PLANTS Database, https://plants.usda.gov. The only value bundled so
far is the PLANTS symbol (a database identifier); the in-app credit is due when taxonomy
or native-status values are imported.

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
