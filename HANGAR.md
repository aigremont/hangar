# Hangar

Hangar helps you tidy a large Second Life inventory without losing anything. It has two halves:

- **A plugin for Radegast Veles** (`plugins/Veles.Plugin.Hangar`). It loads a plan, fetches your live inventory from the server, checks every row of the plan against it, applies the moves in small confirmed batches, and writes a journal so any run can be undone.
- **Python tools** (`tools/inventory`). They read an export of your inventory and write plans: product folders filed by brand and type, vendor landmarks and ad textures trashed, notecards gathered under `Notecards > Docs`, duplicate folders found, empty folders cleared.

Hangar moves folders and items. It never deletes anything: "trash" means "move to the Trash folder", and undo moves things back.

This repository is a fork of [Radegast](https://github.com/cinderblocks/radegast) with the plugin, the tools and an optional compact "Classic" theme for Veles added. Upstream's `README.md` is unchanged apart from a pointer to this file.

## Requirements

- Windows, with the **.NET 10 SDK**.
- **Python 3.11 or newer** for the planning tools. `pip install llsd` is needed only to read raw viewer cache files; Hangar's own export files need nothing extra.
- A Second Life account.
- Optional: **CTS Wardrobe**, if you use the `#RLV` layout below.

## Build and run

From the repository root:

    dotnet build plugins/Veles.Plugin.Hangar -c Release
    dotnet build RadegastVeles -c Release

The plugin DLL is written to `bin/Veles/Release/plugins/`. Copy `Veles.Plugin.Hangar.dll` into the `plugins` folder next to `RadegastVeles.exe` (or run Veles from that build folder), start Veles, then use **Plugins > Plugin Manager > Rescan**. Log in. Only one client can be logged in per account, so a viewer on the same account is logged out.

Run the tests with:

    dotnet test plugins/Veles.Plugin.Hangar.Tests
    python tools/inventory/inventory_triage.py --self-test
    python tools/inventory/inventory_cache_report.py --self-test

## Commands

Type these in the Veles chat box. **The `//` prefix is mandatory.** Without it the line is sent as ordinary local chat that everyone nearby can read. Hangar's replies appear in chat marked `[Hangar]` and are shown only to you.

| Command | What it does |
|---|---|
| `//hangar load` | Pick a plan file. Or give a path with forward slashes: `//hangar load C:/Inventory/plans/plan.csv` (the chat box drops backslashes). Folder plans and action plans are told apart by their header |
| `//hangar fetch` | Fetch every folder and item from the server, so checks run against live data |
| `//hangar check` | Check each row against the live inventory and write `<plan>.preview.csv`. Nothing moves |
| `//hangar apply [N]` | Apply the checked plan, or only its first N rows |
| `//hangar verify` | Ask the server again whether the last moves landed |
| `//hangar undo [journal.tsv]` | Reverse the last journal (or the one named), wherever things are still where Hangar put them |
| `//hangar export` | After a fetch, write the live inventory to one JSON file for the Python tools |
| `//hangar status` | Show what is loaded, fetched and running |
| `//hangar stop` | Stop after the current step |
| `//hangar remote on`, `off`, `status` | Start or stop the local control channel for `tools/inventory/hangar_remote.py` (see `AGENTS.md`). It is off at every login |

`apply` and `check` are one-shot: after an apply, `check` again before applying more.

## Plan formats

Both are UTF-8 CSV and are written by the Python tools. Hangar identifies everything by id, never by name, because names repeat.

**Folder plan** (move product folders). Columns:

    product_folder, items, from, to, because, creates_folder, folder_id, from_id, rename_to, allow_protected

`from` and `to` are folder paths written `A > B > C` below the inventory root. `folder_id` is the folder to move and `from_id` its parent when the plan was made. `rename_to` renames the folder after the move (journalled, so undo restores the old name). `allow_protected` lets a row move into or within `#RLV`, which is otherwise off limits.

**Action plan** (move items, send items or folders to Trash). Columns:

    kind, item_name, asset_type, from, to, because, item_id, from_id

`kind` is `item_move` (needs `to`), `item_trash` or `folder_trash` (`to` blank; `item_id` holds the folder's id for `folder_trash`). Missing destination folders are created, parents first.

## Safe workflow

1. **Fetch.** Log in with Veles and run `//hangar fetch`.
2. **Export.** Run `//hangar export`. It writes `hangar-export-<First_Last>-<date>-<time>.json` next to the last loaded plan, or in Documents.
3. **Plan.** Run the triage tools on the export (see `tools/inventory/README.md`). Each writes a plan and, where useful, a review list.
4. **Dry run.** `//hangar load`, then `//hangar check`. Check touches nothing; it writes `<plan>.preview.csv` showing, per row, whether it will Move, Trash, is AlreadyThere, or is Skipped and why. If you have a spare avatar, rehearse a new kind of plan on it first.
5. **Review.** Read the preview and the review list yourself. Planners make mistakes (see `AGENTS.md`).
6. **Apply 20.** `//hangar apply 20`, then look at the result in the inventory.
7. **Apply.** `//hangar check` again, then `//hangar apply`.
8. **Verify.** `//hangar verify`.
9. **Undo if needed.** Every change is written to a journal (`hangar-apply-<time>.tsv`, next to the plan) *before* it is sent, so even an interrupted run can be undone with `//hangar undo`.
10. **Keep Trash until you are happy.** Do not empty it until you have lived with the result for a while.

Keep your plans, exports, journals and review files in a working folder **outside this repository** and never commit them. They contain your folder and item names and your inventory ids.

## Recommended layout

For items worn through RLV and imported with CTS Wardrobe, use:

    #RLV > Area > Type > Store > Product

for example `#RLV > Clothes > Dress > Some Store > Summer Dress`.

CTS Wardrobe imports **one item per folder** and builds its tags from the **folder names** on the way down. A consistent path therefore gives consistent tags, and the product folder's own name becomes the title, so keep it short and free of the store name, version numbers and body tags. The `rlv` command of `inventory_triage.py` proposes the `Area > Type > Short name` part; you decide the area and type vocabulary and the store folders. Outside `#RLV`, the planner's default is `Category > Brand > Product set`.

## Marketplace pacing

The triage tool can read Marketplace store pages to learn which categories a brand sells in (`--fetch-stores`). Treat the Marketplace gently or it will block you:

- Requests go **one at a time, 2.5 to 5.5 seconds apart**, with a **15 to 30 second pause every 20 requests**.
- **Stop on a block** (HTTP 403 or 429, or a page that is not a Marketplace page). The tool does this and does not retry.
- **Never use the Marketplace search pages.** Only the store pages you list in the stores CSV are fetched, and every page is cached so it is fetched once.

## Known limits

- Objects cannot be moved out of `#RLV` folders by item actions. Only landmarks, textures, notecards and settings items can be moved or trashed from inside `#RLV`; wearables stay put, and `folder_trash` inside `#RLV` is refused. Move whole product folders with a folder plan instead.
- Folders whose names start with `~` (or `.~`) are treated as vendor-managed and are never touched.
- Trash, outfits, Current Outfit, the Marketplace areas, Lost and Found and other system folders, redelivery folders and fitting queues are never touched.
- Moves are confirmed by listing each destination from the server afterwards. Anything the server does not show is reported and left where it was.
- Hangar moves folders and items. It never deletes, never empties Trash, and never edits item contents or permissions.
- The Python tools work from a snapshot (the export). Whatever applies a plan must re-check against the live inventory; Hangar's `check` does.
