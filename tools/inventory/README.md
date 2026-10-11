# Inventory tools

Python 3.11+ planning tools for Hangar. They only read an export and write plans; they never change your inventory. See `HANGAR.md` at the repository root for the whole workflow.

Make an export with `//hangar fetch` then `//hangar export` in Veles. Every tool below accepts the export wherever it accepts a viewer inventory cache (reading a raw cache needs `pip install llsd`). Keep your files in a working folder outside the repository.

    python inventory_triage.py --self-test
    python inventory_cache_report.py --self-test
    python inventory_cache_report.py <export.json>               # aggregate counts only
    python inventory_cache_report.py <export.json> --structure 3

## Plans

    python inventory_triage.py prune   <export.json> <plan.csv>
    python inventory_triage.py docs    <export.json> <plan.csv> [map.json] [--dup-review review.csv]
    python inventory_triage.py dedupe  <export.json> <review.tsv> <plan.csv>
    python inventory_triage.py empties <export.json> <plan.csv> [--under "#RLV"]
    python inventory_triage.py rlv     <export.json> <plan.csv> <review.tsv> [map.json] [--store-cache store_cache.json]

- `prune` trashes vendor landmarks and obvious ad textures inside product folders.
- `docs` moves notecards in product folders to `Notecards > Docs > <Brand>`.
- `dedupe` finds same-name product folders and proposes Trash for boxes and identical copies; the rest is left for review.
- `empties` trashes folders that are empty all the way down below the given roots (default `#RLV`).
- `rlv` proposes `#RLV > Area > Type > Short name` for product folders, with a numbered review list.

All of these are drafts. Review them, then load them with `//hangar load`.

## Guided triage of unfiled folders

    python inventory_triage.py <export.json> --list 30           # brand groups and suggestions
    python inventory_triage.py <export.json>                     # interactive: one decision per brand
    python inventory_triage.py <export.json> --plan plan.csv     # write the folder plan

Decisions are saved to `triage_map.json` in the current folder (`--map` to change it), so a rerun resumes.

## Marketplace store cache (optional)

    python inventory_triage.py <export.json> --stores stores.csv
    python inventory_triage.py <export.json> --fetch-stores stores.csv --store-cache store_cache.json
    python merge_store_cache.py local_cache.json incoming_cache.json

`--stores` writes a CSV of unfiled brands; add each brand's store URL yourself. `--fetch-stores` reads only those store pages, paced at 2.5 to 5.5 seconds per request with a longer rest every 20, and stops at the first block. `--cookie-file` takes your own signed-in Cookie header so adult listings are included; keep that file private and out of version control.

## Remote control

    python hangar_remote.py check
    python hangar_remote.py apply 20
    python hangar_remote.py --list
    python hangar_remote.py --avatar <name> --status

Works only after you type `//hangar remote on` in Veles. See `AGENTS.md`.
