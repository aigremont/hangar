# Instructions for an AI coding agent

You are helping a person organise their Second Life inventory with this repository (Hangar: a Radegast Veles plugin plus Python tools; read `HANGAR.md` first, then `plugins/Veles.Plugin.Hangar/README.md` for the plugin's full reference and `tools/inventory/README.md` for the tools). The inventory is the user's, it is large, and mistakes are expensive. Work carefully, and keep the user in charge of every taste decision.

## Goal and layout conventions

Reach a tidy, predictable tree that the user can navigate and that survives future deliveries:

- Product folders are never split or taken apart. A product folder is one vendor delivery (or one unpacked product), and it moves whole.
- Outside `#RLV`: `Category > Brand > Product set`.
- RLV items: `#RLV > Area > Type > Store > Product`. CTS Wardrobe imports one item per folder and makes its tags from folder names, so the path matters and the product folder should have a short, clean name (no store prefix, version or body tags).
- Boxes (unopened vendor packages) wait in a `To Unpack` area and are not filed as products.
- Vendor landmarks and ad textures do not belong in product folders. Notecards go to `Notecards > Docs > <Brand>`.

## Hard safety rules

1. **Never apply a plan to the user's main avatar without a dry run and a human review.** The dry run is `//hangar check` (it changes nothing and writes `<plan>.preview.csv`). The user reads the preview and the review list. Wait for a clear yes.
2. **Apply 20 first** (`apply 20`), have the user look at the result, then `check` again and apply the rest. Then `verify`.
3. **Journals make undo possible.** Never delete or move a journal (`hangar-apply-*.tsv`). Know that `undo` is available and say so before a large apply.
4. **Never empty Trash for the user.** Hangar's "trash" is a move into the Trash folder. Leave it to the user to empty it, later.
5. **Never handle the user's Second Life password** or ask for it. They log in themselves. Do not read, store or log passwords, tokens or cookie files; if a Marketplace cookie file exists, do not print it and never commit it.
6. **Marketplace pacing.** Serial requests 2.5 to 5.5 seconds apart, a 15 to 30 second pause every 20 requests, stop at the first block (403/429 or a non-Marketplace page), never use the Marketplace search pages. Only the store pages the user listed are fetched. Use the built-in `--fetch-stores`, which does all of this, rather than writing your own crawler.
7. **Ask the user for taste decisions** and write their answers down (for example in their working folder's `decisions.md` or the triage map): which types and area names to use, store names and spellings, what counts as fetishwear or restraints and where it goes, which brands to keep together, what to do with duplicates by version. Do not guess these and do not reverse them later without asking.
8. **Never commit the user's data.** See "Where private data lives".
9. Type nothing for the user into public chat. Hangar commands need the `//` prefix, or the text goes to local chat for everyone nearby. Only the user types `//hangar remote on`.

## Workflow

1. The user logs in with Veles (not a viewer on the same account) and runs `//hangar fetch`, then `//hangar export`.
2. You run the triage tools on the export, writing plans and review lists to the working folder (`plans/`, `review/`).
3. You summarise the plan and the review list for the user. They correct them. You regenerate.
4. The user (or you, over the remote channel, once they have typed `//hangar remote on`) loads the plan and runs `check`.
5. The user reviews `<plan>.preview.csv`. Apply 20, look, check again, apply the rest, verify.
6. Move finished plans and journals to `done/`. Export again before the next round, so the next plan is made from what the server holds now.

### Driving Hangar yourself

After the user types `//hangar remote on` in Veles, `tools/inventory/hangar_remote.py` sends commands over a loopback-only channel (token in `%LOCALAPPDATA%\Hangar\remote-<avatar>.json`):

    python tools/inventory/hangar_remote.py check
    python tools/inventory/hangar_remote.py fetch        # waits until the step finishes
    python tools/inventory/hangar_remote.py apply 20
    python tools/inventory/hangar_remote.py --status
    python tools/inventory/hangar_remote.py --list       # which avatars have remote on

With two Veles sessions (two avatars) running, pass `--avatar <name or part of it>`; without it the client refuses and lists the avatars. It cannot start remote itself, and `apply`, `undo` and `remote off` work only while remote is on. Exit codes: 0 done, 1 the command failed, 2 transport or authorisation error, 3 timed out. Read the returned lines; do not assume success. Do not run `apply` over remote unless the user has approved this exact plan after reading its preview.

## The triage tools

All are in `tools/inventory/`, take a Hangar export (or a viewer cache) and only write plan files. Run `python inventory_triage.py --self-test` after changing any of them.

- `inventory_triage.py <export>`: guided triage of unfiled product folders (in the root, Received Items, Need to Sort and To Unpack style folders). It groups them by brand, files a known brand into its existing home, suggests a home for each new brand, and records one decision per brand in `triage_map.json`. `--list N` shows groups without asking; no flag runs the interactive session; `--plan plan.csv` writes the folder plan; `--stores stores.csv` writes the brands list for Marketplace store URLs; `--fetch-stores stores.csv --store-cache store_cache.json` fetches those store pages with the pacing above.
- `prune <export> <plan.csv>`: action plan trashing vendor landmarks and plain ad textures inside product folders. Notecards, scripts, sounds and anything unsure stay.
- `docs <export> <plan.csv> [map.json] [--dup-review review.csv]`: action plan moving notecards in product folders to `Notecards > Docs > <Brand>`; second copies (same name and creator) become `item_trash` rows, written to the dup-review file if given.
- `dedupe <export> <review.tsv> <plan.csv>`: finds product folders with the same name. A box beside an opened copy: the box is trashed. Identical copies: one kept. Anything else (versions) is left for the user and has no default. The review list shows every group.
- `empties <export> <plan.csv> [--under PATH]`: trashes folders that are empty all the way down below `#RLV` (or each `--under` root). Folders with a link or an item in them, `~` folders and the roots are never listed.
- `rlv <export> <plan.csv> <review.tsv> [map.json] [--store-cache JSON]`: folder plan filing product folders under `#RLV > Area > Type > Short name`, renaming them to short names, with a numbered review list. Products already in place, unopened boxes, husks, vendor-managed folders and body parts are left out.
- `inventory_cache_report.py <export>`: aggregate counts only; safe to paste into a discussion. `--structure N` shows the folder tree to depth N.
- `merge_store_cache.py local.json incoming.json`: merge a store cache fetched elsewhere into the local one, keeping the fuller copy of each store.

## Lessons learned

- **Trust only server-confirmed listings.** A local cache or an old export can contain folders the server no longer has. Fetch, export again, plan from that.
- **Review the planner's output; do not just apply it.** The usual mistakes:
  - brand or wrapper folders mistaken for the product (a folder named for the store holding several products; or a part folder such as `HUDs` or `Shoes` mistaken for a product);
  - brand-only short names (a product renamed to just the brand, or to a generic word), which collide in Wardrobe;
  - boxed versus unpacked decided by **contents, not names**: a folder called `(box)` may hold the opened product, and a plain name may hold one boxed object;
  - duplicates that are really **versions** (v1.2 against v1.3, different body fits), which must not be trashed automatically.
- Names repeat; Hangar uses ids. Never hand-edit a plan to use names instead.
- `~` folders belong to vendors' updaters; leave them alone. Objects cannot be moved out of `#RLV` folders by item actions; use folder plans.
- Apply in small batches and verify. If the server does not confirm something, it stays where it was and is reported; do not retry in a loop.
- Check the work: after an apply, export again and diff the tree, rather than trusting the apply summary alone.

## Where private data lives

Create a **working folder outside this repository** (for example `C:\Inventory\<avatar>\`) with:

    plans/    plan CSVs and their .preview.csv files
    review/   review lists, triage_map.json, decisions notes, stores CSVs, store caches
    done/     applied plans, journals (hangar-apply-*.tsv), old exports

Exports, plans, journals, previews, review lists, store caches, cookie files and decision notes contain the user's folder and item names, brands, avatar names and inventory ids. They must **never be committed** to this repository or pasted into a public place. If you find such a file inside the repository, move it out and tell the user.
