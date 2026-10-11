# Hangar (Veles plugin)

Applies an inventory sorting plan made by `tools/inventory/inventory_triage.py`:
a **folder plan** (move product folders) or an **action plan** (move items, send
items or folders to Trash; see "Item actions" below).
Nothing moves until you have loaded a plan, fetched your inventory live,
checked the plan against it and read the preview. Every change is written to a
journal before it is sent, and `//hangar undo` reverses it.

## Build and install

    dotnet build plugins/Veles.Plugin.Hangar -c Release

The DLL lands in `bin/Veles/Release/plugins/`. Copy `Veles.Plugin.Hangar.dll`
into the `plugins` folder next to `RadegastVeles.exe` (or run Veles from that
build folder), then **Plugins > Plugin Manager > Rescan**.

## Make a plan

    py inventory_triage.py <cache> --map triage_map.json --store-cache store_cache.json --plan plan.csv

The plan needs the `folder_id` and `from_id` columns, written by
`inventory_triage.py`. Product names repeat
(redeliveries), so Hangar moves folders by id, never by name.

## Use it

Log in with Veles (this logs any other viewer out: one client per account). Then type
the commands in the chat box **with the `//` prefix**. Veles treats only
`//`-prefixed text as a command: without it, the line is sent as ordinary
local chat that everyone nearby can read. Hangar's replies show in the chat
window marked `[Hangar]`; those are shown only to you.

| Command | What it does |
|---|---|
| `//hangar load` | Pick the plan file (folder plan or action plan: Hangar tells them apart by the header). Or type a path with **forward slashes** (`//hangar load W:/folder/plan.csv`): the chat box drops backslashes |
| `//hangar fetch` | Fetch every folder from the server, so the check sees live data (batched, 20 folders per request; far faster than folder by folder) |
| `//hangar check` | Check each row against the live inventory and write `<plan>.preview.csv` |
| `//hangar apply 5` | Apply only the first five moves: a safe first try |
| `//hangar apply` | Apply the rest of the checked plan |
| `//hangar verify` | Ask the server again whether the last moves landed |
| `//hangar undo` | Move everything from the last journal back, where still possible |
| `//hangar export` | After a fetch: write the live inventory to one JSON file for the Python tools (see below) |
| `//hangar status`, `hangar stop` | Where things are; stop after the current step |

A row moves only if its folder still exists, still sits where the plan found
it, is not a system folder, is not in a protected area (#RLV and its staging
folders, outfits, Current Outfit, Trash, Marketplace, Lost and Found,
fitting queues, redelivery folders), and its destination is not protected
and not inside the folder itself. Anything else is skipped with a reason in
the preview. Missing destination folders are created, parents first.

**Renames and #RLV.** A decision in the triage map can carry a new name
(`"rename": "Pierced Pasties"`); the plan's `rename_to` column passes it on
and Hangar renames the folder after moving it, journalled like a move, so
undo restores the old name too. A destination you chose yourself inside
#RLV (or another name-protected area) is flagged `allow_protected` in the
plan and allowed; Trash, outfits, Current Outfit, Marketplace and other
system areas stay off limits whatever the plan says, and Hangar never
creates a protected folder that no decision asked for. `allow_protected` also
lets a row move a folder that already sits in #RLV, Wardrobe Import or a fitting
queue (the RLV migration); Trash, system folders, `~` vendor-managed folders and
a folder into itself stay refused.

`apply` and `check` are one-shot: after applying, `check` again before
applying more, so every batch is checked against what is there now.

Moves go out 20 folders per batch, 2 seconds apart. The journal
(`hangar-apply-<time>.tsv`, next to the plan) is written before each batch is
sent, so even an interrupted run can be undone. Undo moves a folder back only
if it is still where Hangar put it; folders Hangar created are left in place.

**Confirmed, not assumed.** Where the region offers the inventory service
(AISv3, the same one the viewer uses), Hangar uses it to create and rename
folders, because it answers for each one:

- Missing folders are created through the service (or reused if a folder of
  that name is already there), and each is looked up on the server before
  anything moves into it. A new folder is journalled with its confirmed id. If
  the server never confirms one (30 s), the apply stops with a message and
  every move is left undone.
- Each rename gets its own answer. A refused one is retried once after 3
  seconds; the ones that still fail are named in chat (first ten, plus a
  count). A folder whose move failed is not renamed.
- Moves are not done through the inventory service: it answers a parent change
  with 400 Bad Request. Hangar sends the viewer's own `MoveInventoryFolder`
  message (up to 20 folders per message), which gets no reply, so the moves
  are confirmed by asking the server for each destination's contents, every 2
  seconds for up to 20 seconds. Folders still missing are sent once more and
  checked again; the ones that still are not there are named in chat (first
  ten, plus a count) and stay where they were. Undo moves the same way.
- Without the inventory service, folders are created over the older UDP
  message, which gives no answer: new folders are polled until they appear,
  and `verify` tells whether the moves landed.

**How fetch lists.** `fetch` asks the server for up to 20 folders per `FetchInventoryDescendents2` request, 8 requests at a time, and prints a progress line every 30 s. A folder the batch leaves unanswered, or whose listing does not add up to the folder's reported descendent count, is listed again on its own (the old one-request-per-folder call), and if the batched request keeps failing the whole fetch falls back to that call. The result is the same either way: only server listings count.

**Only what the server has.** `fetch` records which folders the server
returned, and `check` trusts only those: a folder that sits in the local store
but that the server never returned (for example one made by an older version
over UDP, then restored from Veles' inventory cache) does not exist as far as
the check is concerned, so it is planned as a folder to create. The fetch also
removes such phantom folders from the local store and names them in chat
(ordinary folders only: system folders and folders holding items are reported,
not removed). `check` refuses until a fetch has completed in this session, and
a new fetch drops any earlier check. Before an apply creates or moves anything,
it asks the server whether each existing destination really is there, and
stops with `STOPPED:` if not.

After an apply (and after undo) Hangar refetches every folder it touched, so
the Veles inventory view matches the server even after failed moves, then
verifies. The chat ends with `moved N, failed M, renamed R, created C`.

## Item actions (action plans)

An action plan has this header, exactly, and one row per action:

    kind,item_name,asset_type,from,to,because,item_id,from_id

| `kind` | What it does | `to` |
|---|---|---|
| `item_move` | Moves item `item_id` (now in folder `from_id`, path `from`) to the folder path `to`, creating the missing folders along it | destination path, `A > B > C` |
| `item_trash` | Moves the item to the Trash folder | blank |
| `folder_trash` | Moves folder `item_id` (a folder id here) to the Trash folder | blank |

`load` reads the header: a file with `kind` and `item_id` columns is an action
plan, anything else is a folder plan. An unknown `kind` stops the load with the
row number. Every other command (`fetch`, `check`, `apply [N]`, `verify`, `undo`,
`export`, `status`, `stop`) works the same for both formats.

Items inside an `#RLV` folder are normally off limits, but a Landmark, Texture,
Notecard or Settings item may be moved or trashed out of it (the type is the
server's; a row whose `asset_type` disagrees is skipped). Wearables stay put, and
`folder_trash` inside `#RLV` is still refused.

**Nothing is ever deleted.** A trash is a move into Trash, and `undo` moves the
item back out. Do not empty Trash until the batch is verified.

`check` writes `<plan>.preview.csv` with the columns `outcome,kind,item_name,
asset_type,from,to,reason,item_id` and these outcomes:

| Outcome | Meaning |
|---|---|
| `Move` | The item will move to `to` (`move (creates folders)` if folders must be made first) |
| `Trash` | The item or folder will go to Trash |
| `AlreadyThere` | An `item_move` whose item is already in `to`, or a trash of something already in Trash (in the Trash folder or anywhere below it) |
| `Skip` | Not done, with the reason: no longer in the inventory, moved since the plan was made, in a protected area, no destination, listed twice |
| `CreateFolder` | A destination folder that does not exist yet (parents first) |

The rules are the folder plan's: an item is skipped if its folder is a system
area (Trash, outfits, Current Outfit, Marketplace, Lost and Found, Landmarks,
Calling Cards, Favorites) or is in or under a protected name (#RLV, redelivery
folders, fitting queues); a `folder_trash` row is also skipped for a system
folder. Items may be filed into an *existing* system folder such as `Notecards`
(`Notecards > Docs > <Brand>` is created below it) and into an *existing*
folder under a protected name such as #RLV (the preview says "into an existing
protected folder"); Hangar never creates folders inside a protected area or
under a protected name, and never files into Trash, outfits or the other
system areas with `item_move`. An action plan has no `allow_protected` column.

Items are looked up by id in what the server listed during `fetch` (the fetch
now records every item it sees), never in the local store. A row whose item is
not in that listing is skipped as "no longer in the inventory".

`apply [N]` applies the first N rows that will do something. It creates the
missing destination folders (same confirmed create as folder plans), moves the
items, then the folders going to Trash. Item moves are sent with the viewer's
own `MoveInventoryItem` message (30 per message, 2 seconds apart, no
inventory-service call), journalled before each message, with a progress line
every 30 seconds. The message has no reply, so each destination folder is then
listed from the server (items included) every 2 seconds for up to 20 seconds;
items not listed there are sent once more and checked again, and the ones that
still are not there are named in chat and stay where they were. Folders going to
Trash move exactly like folder moves. The chat ends with `moved N items,
trashed T items and F folders, failed M, created C`, then verifies by listing
the destinations again.

`undo` moves items back to the folder they first came from, and folders back as
before, but only if each is still where Hangar put it (as the server lists it)
and its old folder still exists. The journal's `item` entries (kind `Item`)
carry the item id in the `folder_id` column.

## Export for the Python tools

`//hangar export` needs a completed `//hangar fetch` in this session. It walks
the folders the server confirmed (the Library is never included), looks up the
creators' names, and writes `hangar-export-<First_Last>-<yyyyMMdd-HHmmss>.json`
next to the last loaded plan (the `last_plan` setting), or in Documents if
there is none. Chat ends with `exported N folders, M items, C creators (U
unresolved) to <path>`. Items in a folder the fetch got no answer for are
missing; a warning says so.

Creator names come from the display-name service (`GetDisplayNamesAsync`, 50
ids per request, 0.5 s apart, 60 s in all), with the older name packets as a
fallback. Ids that do not resolve are left out of `creators`.

`inventory_cache_report.py` and `inventory_triage.py` accept the file wherever
they accept a cache (`py inventory_triage.py hangar-export-....json --list`).
The reader maps the names below to the cache's strings and keeps the creator
names (`inv.creator_names`).

    {"format": "hangar-export", "version": 1, "exported": "2026-10-02T12:00:00+01:00",
     "agent_id": "...", "agent_name": "First Last", "root_id": "...",
     "folders": [{"id", "parent", "name", "type"}],      // type: LibreMetaverse FolderType name, "None" = ordinary
     "items": [{"id", "parent", "name", "asset_type", "inv_type",
                "wearable_type",                          // wearables only
                "creator", "owner", "flags",
                "perms": {"owner", "next_owner"},         // PermissionMask as integers
                "is_link", "link_target"}],               // link_target only for links
     "creators": {"<uuid>": "Legacy Name (display name)"}}

`asset_type` and `inv_type` are LibreMetaverse `AssetType` and `InventoryType`
names. The root folder is listed with parent `00000000-0000-0000-0000-000000000000`.

## Remote control

A local control channel, so an assistant on the same machine can run Hangar
commands and read their output without typing in the chat box. It is **off at
every login** and does nothing until you type, in Veles chat:

    //hangar remote on        start it (also: off, status)

`on` starts an HTTP listener on `http://127.0.0.1:48213/` (loopback only: other
machines cannot reach it) and says the port it chose in chat. If that port is
taken (say, by a second Veles with another avatar), the next ones up to +9 are
tried; `HANGAR_REMOTE_PORT` sets the first port to try. It writes
`%LOCALAPPDATA%\Hangar\remote-<avatar>.json` (the avatar name with every
non-alphanumeric replaced by `_`, e.g. `remote-Jane_Example.json`) holding
`port`, a fresh random token, `avatar` and `pid`. Every request needs
`Authorization: Bearer <token>`, else 401. `off`, logout and unloading the plugin
stop the listener and delete that avatar's own file only; on plugin load, any
`remote-*.json` whose `pid` is not a running process (left by a crash) is deleted.
No `netsh` URL reservation is needed for `127.0.0.1`.

Two instances (two avatars) can have remote on at the same time, each with its
own port and file.

| Request | Does |
|---|---|
| `POST /command` `{"text": "check"}` | Runs the text after `//hangar` exactly as chat would. Short commands return `{"ok":true,"started":false,"lines":[...],"next":N}`; `fetch`, `apply`, `verify`, `undo`, `export` return `"started":true` at once, and the progress is read from `/lines` |
| `GET /lines?since=N` | The last 2000 lines Hangar said, each `{seq, text}`, those after `N`; also `next`, `busy`, `truncated` |
| `GET /status` | `{"avatar", "loaded" (plan path or null), "fetched", "busy"}` |

Rules: a command is refused with 403 while another runs (only `stop` gets through),
`remote on` cannot be issued over HTTP, and `apply`, `undo` and `remote off` over
HTTP work only while remote is on, which only chat can switch on. Everything chat
refuses (apply before check, ...) is refused the same way, in the returned lines.
Each command is echoed to local chat as `[Hangar] remote: <text>`, with its output.

The assistant's client is `tools/inventory/hangar_remote.py` (Python 3, stdlib only):

    python hangar_remote.py check
    python hangar_remote.py fetch          # polls /lines every 2 s until it finishes
    python hangar_remote.py --status
    python hangar_remote.py --list                  # avatar, port, pid of each remote that is on
    python hangar_remote.py --avatar alex check    # pick an instance

With exactly one token file the client uses it. With several and no `--avatar`
it exits 2 and lists the avatars; `--avatar` takes a name or a fragment of it,
case-insensitive. `--status` with none on prints "no Hangar remote is on".
It exits non-zero on an error, a 401/403, or after 30 minutes.

## Tests

    dotnet test plugins/Veles.Plugin.Hangar.Tests

The core (`Core/`) has no Veles or LibreMetaverse dependency and is tested on
its own; `Live/` is the thin layer over LibreMetaverse.
