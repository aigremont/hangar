#!/usr/bin/env python3
"""\
@file   inventory_cache_report.py
@brief  Read-only summary of a viewer inventory cache, for planning
        inventory organisation tools.

        The viewer writes the whole agent inventory to its cache folder at
        logout (LLInventoryModel::saveToFile): a gzip of a 4-byte big-endian
        cache version followed by binary LLSD holding "categories" and
        "items" arrays. This script reads that file and prints aggregate
        numbers only -- counts, shares and histograms. The report never
        contains item names, folder names, descriptions or UUIDs unless asked
        to with --top-creators, so it is safe to paste into a discussion.
        (Given a folder holding several caches, it lists their file names on
        stderr so you can pick one; those names contain avatar UUIDs and are
        not part of the report.)

        It opens files for reading and writes nothing.

        Where to find the file (default cache locations; if you moved the
        cache, use the folder shown in Preferences):
            Windows  %LOCALAPPDATA%\\AlchemyNext\\
            macOS    ~/Library/Caches/AlchemyNext/
            Linux    the viewer's user folder, under cache/
        Some builds put the file in an inv_cache subfolder of that; pointing
        the script at the cache folder checks both. The file is named <your-avatar-uuid>.inv.llsd.gz (on non-production
        grids, <uuid>.<grid>.inv.llsd.gz). Log out cleanly first so the file
        is current, and let the inventory finish loading in that session so
        the file is complete.

        Usage:
            python tools/inventory/inventory_cache_report.py <file-or-folder>
            python tools/inventory/inventory_cache_report.py <file> --json
            python tools/inventory/inventory_cache_report.py <file> --top-creators 20
            python tools/inventory/inventory_cache_report.py --self-test

        Needs the llsd package (pip install llsd), which is already listed
        in the repository's requirements.txt.

$LicenseInfo:firstyear=2026&license=viewerlgpl$
Alchemy Viewer Source Code
Copyright (C) 2026, Alchemy Viewer Project

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation;
version 2.1 of the License only.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
$/LicenseInfo$
"""

import argparse
import datetime
import glob
import gzip
import json
import os
import re
import struct
import sys
import uuid
from collections import Counter, defaultdict

try:
    import llsd
except ImportError:
    sys.exit("the llsd package is required: pip install llsd")


# LLInventoryModel::sCurrentInvCacheVersion for the binary layout, and the
# last version of the older text layout.
EXPECTED_CACHE_VERSION = 5
KNOWN_CACHE_VERSIONS = (3, 5)

# llpermissionsflags.h
PERM_TRANSFER = 1 << 13
PERM_MODIFY = 1 << 14
PERM_COPY = 1 << 15

# llinventorydefines.h
II_FLAGS_SUBTYPE_MASK = 0x0000ff
II_FLAGS_OBJECT_HAS_MULTIPLE_ITEMS = 0x200000

# The key LLInventoryItem::asLLSD XORs a restricted item's asset id with
# before writing it as shadow_id (llinventory.cpp, MAGIC_ID).
MAGIC_ID = uuid.UUID("3c115e51-04f4-523c-9fa6-98aff1034730")

# LLFolderType names (llfoldertype.cpp) for the folders the report groups by.
SYSTEM_FOLDER_LABELS = {
    "root_inv": "My Inventory (root)",
    "trash": "Trash",
    "current": "Current Outfit",
    "my_otfts": "My Outfits",
    "inbox": "Received Items",
    "merchant": "Marketplace Listings",
    "outbox": "Merchant Outbox",
    "lstndfnd": "Lost And Found",
    "favorite": "Favorites",
    "callcard": "Calling Cards",
    "landmark": "Landmarks",
    "object": "Objects",
    "clothing": "Clothing",
    "bodypart": "Body Parts",
    "texture": "Textures",
    "snapshot": "Photo Album",
    "notecard": "Notecards",
    "lsltext": "Scripts",
    "sound": "Sounds",
    "animatn": "Animations",
    "gesture": "Gestures",
    "settings": "Settings",
    "material": "Materials",
    "animover": "Animation Overrides",
    "rlv": "#RLV",
}

# Folders the viewer files new items into by type. Items sitting directly in
# one of these have usually never been sorted.
DEFAULT_TYPE_FOLDERS = {
    "object", "clothing", "bodypart", "texture", "snapshot", "notecard",
    "lsltext", "sound", "animatn", "gesture", "settings", "material",
    "landmark", "callcard",
}

USER_ZONE = "your own folders"
NULL_ID = str(uuid.UUID(int=0))
ORPHAN_ZONE = "(parent folder not in cache)"

# Zones an organising tool must not reorganise; their items are left out of
# the per-item statistics and only counted in the zone table.
EXCLUDED_ZONES = {
    "Trash", "Current Outfit", "Marketplace Listings", "Merchant Outbox",
    ORPHAN_ZONE,
}

# LLAssetType names (llassettype.cpp).
ASSET_TYPE_LABELS = {
    "texture": "texture", "sound": "sound", "callcard": "calling card",
    "landmark": "landmark", "script": "script (legacy)", "clothing": "clothing",
    "object": "object", "notecard": "notecard", "lsltext": "script",
    "bodypart": "body part", "animatn": "animation", "gesture": "gesture",
    "link": "link", "link_f": "folder link", "mesh": "mesh",
    "settings": "settings", "material": "material", "gltf": "gltf",
}

# LLWearableType::EType, for clothing and body parts (low byte of flags).
WEARABLE_TYPES = [
    "shape", "skin", "hair", "eyes", "shirt", "pants", "shoes", "socks",
    "jacket", "gloves", "undershirt", "underpants", "skirt", "alpha",
    "tattoo", "physics", "universal",
]

# Heuristics. None of these are authoritative; they exist to size problems.
BODY_NAMES_RE = re.compile(
    r"\b(maitreya|lara\s*x?|petite|perky|legacy|reborn|kupra|belleza|freya|"
    r"isis|venus|jake|gianni|geralt|slink|hourglass|physique|inithium|"
    r"ebody|altamura)\b",
    re.IGNORECASE)
BOXED_RE = re.compile(
    r"\b(box(ed)?|unpack(er|ing)?|packag(e|ed)|crate|rez\s*(me|to\s*unpack)|"
    r"open\s*me)\b",
    re.IGNORECASE)
BRAND_PREFIX_RE = re.compile(
    r"^\s*(\[[^\]]+\]|\([^)]+\)|\{[^}]+\}|<[^>]+>|"
    r"[:.~*•♥★]+\s*[^:.~*•♥★]+?\s*[:.~*•♥★]+)")
BRACKETED_RE = re.compile(r"[\[\(\{<][^\]\)\}>]*[\]\)\}>]")
VERSION_RE = re.compile(r"\bv(er(sion)?)?\s*\d+(\.\d+)*\b|\b\d+(\.\d+)+\b",
                        re.IGNORECASE)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# A folder counts as product-like when this share of its direct items comes
# from one creator (vendor deliveries usually arrive as one folder).
PRODUCT_CREATOR_SHARE = 0.8
BOILERPLATE_MIN_REPEATS = 10


# --------------------------------------------------------------------------
# Loading

# The Hangar plugin's "//hangar export" file (plugins/Veles.Plugin.Hangar,
# README): JSON written from the live inventory. Its names are LibreMetaverse
# enum names; these tables turn them into the strings the cache uses.
EXPORT_FORMAT = "hangar-export"

# LibreMetaverse FolderType -> LLFolderType name. "RootInventory" and
# "Bodypart" are accepted as aliases of the library's own spellings.
EXPORT_FOLDER_TYPES = {
    "None": "-1", "Root": "root_inv", "RootInventory": "root_inv",
    "OldRoot": "root_inv", "Trash": "trash", "Inbox": "inbox",
    "CurrentOutfit": "current", "MyOutfits": "my_otfts", "Outfit": "outfit",
    "LostAndFound": "lstndfnd", "Favorites": "favorite",
    "CallingCard": "callcard", "Landmark": "landmark",
    "MarketplaceListings": "merchant", "Outbox": "outbox",
    "MarketplaceStock": "stock", "MarketplaceVersion": "version",
    "Object": "object", "Clothing": "clothing", "BodyPart": "bodypart",
    "Bodypart": "bodypart", "Material": "material", "Settings": "settings",
    "Texture": "texture", "Animation": "animatn", "Gesture": "gesture",
    "Notecard": "notecard", "LSLText": "lsltext", "Snapshot": "snapshot",
    "Sound": "sound", "Mesh": "mesh", "BasicRoot": "basic_rt",
    "Suitcase": "suitcase",
}

# LibreMetaverse AssetType -> LLAssetType name.
EXPORT_ASSET_TYPES = {
    "Texture": "texture", "Sound": "sound", "CallingCard": "callcard",
    "Landmark": "landmark", "Script": "script", "Clothing": "clothing",
    "Object": "object", "Notecard": "notecard", "Folder": "category",
    "LSLText": "lsltext", "LSLBytecode": "lslbyte", "TextureTGA": "txtr_tga",
    "Bodypart": "bodypart", "BodyPart": "bodypart", "SoundWAV": "snd_wav",
    "ImageTGA": "img_tga", "ImageJPEG": "jpeg", "Animation": "animatn",
    "Gesture": "gesture", "Simstate": "simstate", "Link": "link",
    "LinkFolder": "link_f", "Mesh": "mesh", "Settings": "settings",
    "Material": "material", "GLTF": "gltf", "GLTFBin": "glbin",
    "Widget": "widget", "Person": "person", "Unknown": "unknown",
}

# LibreMetaverse InventoryType -> LLInventoryType name.
EXPORT_INV_TYPES = {
    "Texture": "texture", "Sound": "sound", "CallingCard": "callcard",
    "Landmark": "landmark", "Object": "object", "Notecard": "notecard",
    "Category": "category", "RootCategory": "root", "LSL": "script",
    "Snapshot": "snapshot", "Attachment": "attach", "Wearable": "wearable",
    "Animation": "animation", "Gesture": "gesture", "Mesh": "mesh",
    "Widget": "widget", "Person": "person", "Settings": "settings",
    "Material": "material", "GLTF": "gltf", "GLTFBin": "glbin",
    "Unknown": "",
}


class ExportMeta(int):
    """The "version" read_cache returns for an export: the current cache
    version as an int, so reports treat it like a cache, with the export's
    own facts as attributes. creators maps lower-case creator UUID to
    "Legacy Name (display name)" for the creators that were resolved."""

    def __new__(cls, export):
        self = int.__new__(cls, EXPECTED_CACHE_VERSION)
        self.source = EXPORT_FORMAT
        self.export_version = export.get("version")
        self.exported = export.get("exported")
        self.agent_id = export.get("agent_id")
        self.agent_name = export.get("agent_name")
        self.creators = {str(k).lower(): v
                         for k, v in (export.get("creators") or {}).items()}
        return self


def _is_export(path, data):
    if path.lower().endswith(".json"):
        return True
    head = data[:100].lstrip(b"\xef\xbb\xbf \r\n\t")
    return head[:1] == b"{" and b'"format"' in head and EXPORT_FORMAT.encode() in head


def _read_export(data):
    export = json.loads(data.decode("utf-8-sig"))
    if not isinstance(export, dict) or export.get("format") != EXPORT_FORMAT:
        raise ValueError("not a hangar-export file")
    if export.get("version") != 1:
        raise ValueError("unsupported hangar-export version %r" % export.get("version"))
    owner = as_uuid(export.get("agent_id"))
    categories = []
    for f in export.get("folders", []):
        ptype = f.get("type", "None")
        categories.append({
            "cat_id": as_uuid(f.get("id")),
            "parent_id": as_uuid(f.get("parent")) or uuid.UUID(int=0),
            "type": "category",
            "preferred_type": EXPORT_FOLDER_TYPES.get(ptype, str(ptype).lower()),
            "name": f.get("name", ""), "owner_id": owner, "version": 1})
    items = []
    for i in export.get("items", []):
        perms = i.get("perms") or {}
        creator = as_uuid(i.get("creator")) or uuid.UUID(int=0)
        atype = i.get("asset_type", "Unknown")
        itype = i.get("inv_type", "Unknown")
        sd = {
            "item_id": as_uuid(i.get("id")), "parent_id": as_uuid(i.get("parent")),
            "permissions": {
                "creator_id": creator,
                "owner_id": as_uuid(i.get("owner")) or owner,
                "owner_mask": int(perms.get("owner", 0) or 0),
                "next_owner_mask": int(perms.get("next_owner", 0) or 0)},
            "type": EXPORT_ASSET_TYPES.get(atype, str(atype).lower()),
            "inv_type": EXPORT_INV_TYPES.get(itype, str(itype).lower()),
            "flags": int(i.get("flags", 0) or 0),
            "name": i.get("name", ""), "desc": ""}
        if i.get("wearable_type"):
            sd["wearable_type"] = i["wearable_type"]
        if i.get("is_link") and as_uuid(i.get("link_target")):
            sd["asset_id"] = as_uuid(i["link_target"])
        items.append(sd)
    return ExportMeta(export), categories, items

def read_cache(path):
    """Return (cache_version, categories, items) from an inventory cache.

    A hangar-export .json file (the Hangar plugin's live export) is also
    accepted and converted to the same shape; its first element is an
    ExportMeta, an int with a .creators map of creator UUID to name.

    Two layouts exist. Current viewers write a 4-byte big-endian version and
    one binary LLSD map (cache version 5). Older ones, including some
    released Alchemy builds, write LLSD notation text: a first line
    {'inv_cache_version':i3}, then one folder or item map per line.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if _is_export(path, data):
        return _read_export(data)
    if data[:1] == b"{":
        return _read_notation_cache(data)
    if len(data) < 4:
        raise ValueError("file is too short to be an inventory cache")
    (version,) = struct.unpack(">I", data[:4])
    inventory = llsd.parse_binary(data[4:])
    if not isinstance(inventory, dict):
        raise ValueError("cache body is not an LLSD map")
    return version, inventory.get("categories", []), inventory.get("items", [])


def _read_notation_cache(data):
    version, categories, items = None, [], []
    pending = b""
    for line in data.split(b"\n"):
        line = pending + line
        if not line.strip():
            continue
        try:
            sd = llsd.parse_notation(line.strip())
        except llsd.LLSDParseError:
            # A value spanning lines; keep reading until it parses.
            pending = line + b"\n"
            continue
        pending = b""
        if not isinstance(sd, dict):
            continue
        if "inv_cache_version" in sd:
            version = int(sd["inv_cache_version"])
        elif "cat_id" in sd:
            categories.append(sd)
        elif "item_id" in sd:
            items.append(sd)
    if pending.strip():
        raise ValueError("unparseable record at end of file")
    if version is None:
        raise ValueError("no inv_cache_version line: not an inventory cache")
    return version, categories, items


def read_name_cache(path):
    """Map creator UUID -> readable name from avatar_name_cache*.xml."""
    with open(path, "rb") as f:
        data = llsd.parse_xml(f.read())
    names = {}
    for agent_id, entry in (data.get("agents") or {}).items():
        name = entry.get("username") or " ".join(
            p for p in (entry.get("legacy_first_name"),
                        entry.get("legacy_last_name")) if p)
        display = entry.get("display_name")
        if display and display != name:
            name = "%s (%s)" % (display, name)
        if name:
            names[str(agent_id).lower()] = name
    return names


def as_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def link_target(item):
    """Target id of a link item, undoing the shadow_id XOR if needed."""
    target = as_uuid(item.get("asset_id"))
    if target is not None:
        return target
    shadow = as_uuid(item.get("shadow_id"))
    if shadow is None:
        return None
    return uuid.UUID(bytes=bytes(a ^ b for a, b in zip(shadow.bytes, MAGIC_ID.bytes)))


def item_flags(item):
    flags = item.get("flags", 0)
    if isinstance(flags, (bytes, bytearray)):
        return struct.unpack(">I", bytes(flags)[:4].rjust(4, b"\0"))[0]
    try:
        return int(flags)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Analysis

class Folder(object):
    __slots__ = ("id", "parent", "ptype", "name", "depth", "zone",
                 "children", "items", "product_like", "category_like")

    def __init__(self, sd):
        self.id = as_uuid(sd.get("cat_id"))
        self.parent = as_uuid(sd.get("parent_id"))
        self.ptype = str(sd.get("preferred_type", "-1"))
        self.name = sd.get("name", "")
        self.depth = None
        self.zone = None
        self.children = []
        self.items = []
        self.product_like = False
        self.category_like = False


def infer_unsaved_objects_folder(folders, items=()):
    """Older viewers write only folders whose version they have checked, so
    the system Objects folder's own record can be missing while everything
    under it is saved. If every detached folder hangs off one missing parent
    and the root has no Objects folder, recreate that parent as Objects."""
    root = next((f for f in folders.values() if f.ptype == "root_inv"), None)
    if root is None:
        return False
    missing = {f.parent for f in folders.values()
               if f is not root and f.parent not in folders}
    missing |= {as_uuid(sd.get("parent_id")) for sd in items
                if as_uuid(sd.get("parent_id")) not in folders}
    has_objects = any(f.ptype == "object" and f.parent == root.id
                      for f in folders.values())
    if len(missing) != 1 or has_objects:
        return False
    (parent_id,) = missing
    folders[parent_id] = Folder({"cat_id": parent_id, "parent_id": root.id,
                                 "preferred_type": "object", "name": "Objects"})
    return True


def resolve_folders(folders):
    """Assign depth and zone to every folder; return the root folder."""
    root = next((f for f in folders.values() if f.ptype == "root_inv"), None)
    if root is None:
        return None
    root.depth, root.zone = 0, SYSTEM_FOLDER_LABELS["root_inv"]
    for folder in folders.values():
        chain = []
        cur = folder
        seen = set()
        while cur is not None and cur.depth is None and cur.id not in seen:
            seen.add(cur.id)
            chain.append(cur)
            cur = folders.get(cur.parent)
        if cur is None or cur.depth is None:
            # Chain left the cache (or looped) before reaching the root.
            for f in chain:
                f.depth, f.zone = -1, ORPHAN_ZONE
            continue
        for f in reversed(chain):
            f.depth = cur.depth + 1
            if cur is root:
                if f.ptype in SYSTEM_FOLDER_LABELS:
                    f.zone = SYSTEM_FOLDER_LABELS[f.ptype]
                elif f.name.strip().lower() == "#rlv":
                    f.zone = "#RLV"
                else:
                    f.zone = USER_ZONE
            else:
                f.zone = cur.zone
            cur = f
    return root


def normalise_name(name):
    s = BRACKETED_RE.sub(" ", name.lower())
    s = BODY_NAMES_RE.sub(" ", s)
    s = VERSION_RE.sub(" ", s)
    return NON_ALNUM_RE.sub(" ", s).strip()


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def bucket(n, edges):
    for lo, hi, label in edges:
        if lo <= n <= hi:
            return label
    return edges[-1][2]


FANOUT_EDGES = [(0, 0, "0"), (1, 10, "1-10"), (11, 50, "11-50"),
                (51, 200, "51-200"), (201, 1000, "201-1000"),
                (1001, 10 ** 12, ">1000")]


def analyse(version, categories, items, names=None, top_creators=0):
    report = {"cache_version": version,
              "cache_version_current": version in KNOWN_CACHE_VERSIONS}

    folders = {}
    for sd in categories:
        folder = Folder(sd)
        if folder.id is not None:
            folders[folder.id] = folder
    inferred = infer_unsaved_objects_folder(folders, items)
    root = resolve_folders(folders)
    report["has_root_folder"] = root is not None
    if root is None:
        return report
    for folder in folders.values():
        parent = folders.get(folder.parent)
        if parent is not None:
            parent.children.append(folder)

    object_ids = set(folders)
    parsed = []
    for sd in items:
        item_id = as_uuid(sd.get("item_id"))
        if item_id is None:
            continue
        object_ids.add(item_id)
        parent = folders.get(as_uuid(sd.get("parent_id")))
        zone = parent.zone if parent is not None else ORPHAN_ZONE
        perms = sd.get("permissions") or {}
        parsed.append({
            "sd": sd,
            "parent": parent,
            "zone": zone,
            "type": str(sd.get("type", "")),
            "inv_type": str(sd.get("inv_type", "")),
            "name": sd.get("name", "") or "",
            "desc": sd.get("desc", "") or "",
            "creator": str(as_uuid(perms.get("creator_id")) or ""),
            "owner": str(as_uuid(perms.get("owner_id")) or ""),
            "owner_mask": int(perms.get("owner_mask", 0) or 0),
            "flags": item_flags(sd),
            "created": int(sd.get("created_at", 0) or 0),
        })

    links = [p for p in parsed if p["type"] in ("link", "link_f")]
    real = [p for p in parsed if p["type"] not in ("link", "link_f")]
    scope = [p for p in real if p["zone"] not in EXCLUDED_ZONES]
    n_scope = len(scope)

    # Completeness -------------------------------------------------------
    orphan_items = sum(1 for p in parsed if p["zone"] == ORPHAN_ZONE)
    orphan_folders = sum(1 for f in folders.values() if f.zone == ORPHAN_ZONE)
    if orphan_items or orphan_folders:
        note = ("items or folders whose parent is missing usually mean the "
                "last session had not finished loading inventory")
    elif inferred:
        note = ("complete; the Objects folder's own record was not saved (older "
                "viewers skip folders they have not version-checked), so its "
                "contents were reattached to an inferred Objects folder")
    else:
        note = "looks complete"
    report["completeness"] = {
        "items_with_parent_missing": orphan_items,
        "folders_not_connected_to_root": orphan_folders,
        "objects_folder_inferred": inferred,
        "note": note,
    }

    # Size and shape -----------------------------------------------------
    connected = [f for f in folders.values() if f.depth is not None and f.depth >= 0]
    depth_hist = Counter()
    for p in scope:
        if p["parent"] is not None:
            depth_hist[p["parent"].depth] += 1
    report["size"] = {
        "folders": len(folders),
        "items_including_links": len(parsed),
        "items_excluding_links": len(real),
        "links": len(links),
        "items_in_scope": n_scope,
        "items_in_scope_note": "excludes links and anything in " + ", ".join(
            sorted(z for z in EXCLUDED_ZONES)),
        "max_folder_depth": max((f.depth for f in connected), default=0),
        "items_by_folder_depth": dict(sorted(depth_hist.items())),
    }

    # Zones --------------------------------------------------------------
    zone_items = Counter(p["zone"] for p in real)
    zone_links = Counter(p["zone"] for p in links)
    zone_folders = Counter(f.zone for f in folders.values())
    report["zones"] = {
        zone: {"items": zone_items.get(zone, 0),
               "links": zone_links.get(zone, 0),
               "folders": zone_folders.get(zone, 0)}
        for zone in sorted(set(zone_items) | set(zone_links) | set(zone_folders),
                           key=lambda z: -(zone_items.get(z, 0) + zone_links.get(z, 0)))
    }

    # Item types ---------------------------------------------------------
    types = Counter(ASSET_TYPE_LABELS.get(p["type"], p["type"] or "?") for p in scope)
    objects = [p for p in scope if p["type"] == "object"]
    wearables = Counter()
    for p in scope:
        if p["type"] in ("clothing", "bodypart"):
            sub = p["flags"] & II_FLAGS_SUBTYPE_MASK
            wearables[WEARABLE_TYPES[sub] if sub < len(WEARABLE_TYPES) else "other"] += 1
    report["types"] = {
        "by_type": dict(types.most_common()),
        "objects_last_worn_as_attachment": sum(1 for p in objects if p["inv_type"] == "attach"),
        "objects_coalesced": sum(1 for p in objects
                                 if p["flags"] & II_FLAGS_OBJECT_HAS_MULTIPLE_ITEMS),
        "wearables_by_kind": dict(wearables.most_common()),
    }

    # Permissions (what the owner can do) ---------------------------------
    no_mod = sum(1 for p in scope if not p["owner_mask"] & PERM_MODIFY)
    no_copy = sum(1 for p in scope if not p["owner_mask"] & PERM_COPY)
    no_trans = sum(1 for p in scope if not p["owner_mask"] & PERM_TRANSFER)
    combos = Counter()
    for p in scope:
        m = p["owner_mask"]
        combos["/".join(label if m & bit else "no-" + label
                        for bit, label in ((PERM_MODIFY, "mod"), (PERM_COPY, "copy"),
                                           (PERM_TRANSFER, "trans")))] += 1
    report["permissions"] = {
        "no_modify": no_mod, "no_modify_pct": pct(no_mod, n_scope),
        "no_copy": no_copy, "no_copy_pct": pct(no_copy, n_scope),
        "no_transfer": no_trans, "no_transfer_pct": pct(no_trans, n_scope),
        "combinations": dict(combos.most_common()),
        "note": "no-modify items cannot be renamed or have their description edited",
    }

    # Links --------------------------------------------------------------
    broken = [p for p in links if link_target(p["sd"]) not in object_ids]
    broken_zones = Counter(p["zone"] for p in broken)
    report["links"] = {
        "total": len(links),
        "folder_links": sum(1 for p in links if p["type"] == "link_f"),
        "target_not_in_cache": len(broken),
        "target_not_in_cache_by_zone": dict(broken_zones.most_common()),
        "note": ("a missing target is usually a deleted item, but can also be "
                 "Library content or a folder the session had not loaded"),
    }

    # Creators -----------------------------------------------------------
    by_creator = Counter(p["creator"] for p in scope
                         if p["creator"] and p["creator"] != NULL_ID)
    ranked = by_creator.most_common()
    self_made = sum(1 for p in scope
                    if p["creator"] not in ("", NULL_ID) and p["creator"] == p["owner"])

    def top_share(n):
        return pct(sum(c for _, c in ranked[:n]), n_scope)

    report["creators"] = {
        "distinct_creators": len(by_creator),
        "items_you_created": self_made,
        "items_you_created_pct": pct(self_made, n_scope),
        "top_10_creators_share_pct": top_share(10),
        "top_50_creators_share_pct": top_share(50),
        "top_100_creators_share_pct": top_share(100),
        "creators_with_20_plus_items": sum(1 for _, c in ranked if c >= 20),
        "creators_with_1_item": sum(1 for _, c in ranked if c == 1),
        "items_with_no_creator_recorded": sum(1 for p in scope if p["creator"] == NULL_ID),
    }
    if top_creators:
        names = names or {}
        report["creators"]["top"] = [
            {"creator": names.get(cid, cid), "items": count}
            for cid, count in ranked[:top_creators]]

    # Folder patterns ----------------------------------------------------
    for p in scope:
        if p["parent"] is not None:
            p["parent"].items.append(p)
    candidates = [f for f in folders.values()
                  if f.ptype == "-1" and f.zone not in EXCLUDED_ZONES
                  and f.zone != "My Outfits"]
    for f in candidates:
        creators = Counter(p["creator"] for p in f.items)
        if len(f.items) >= 2 and len(f.children) <= 3:
            top = creators.most_common(1)[0][1]
            f.product_like = top >= PRODUCT_CREATOR_SHARE * len(f.items)
    for f in candidates:
        kids = [c for c in f.children if c.ptype == "-1"]
        product_kids = sum(1 for c in kids if c.product_like)
        f.category_like = (
            (len(kids) >= 3 and product_kids >= 0.5 * len(kids))
            or (len(f.items) >= 10 and len({p["creator"] for p in f.items}) >= 5))

    def in_product_folder(folder):
        cur, seen = folder, set()
        while cur is not None and cur.id not in seen and cur.depth and cur.depth > 0:
            if cur.product_like:
                return True
            seen.add(cur.id)
            cur = folders.get(cur.parent)
        return False

    memo = {}
    items_in_products = 0
    for p in scope:
        f = p["parent"]
        if f is None:
            continue
        if f.id not in memo:
            memo[f.id] = in_product_folder(f)
        items_in_products += memo[f.id]

    fanout = Counter(bucket(len(f.items), FANOUT_EDGES)
                     for f in folders.values() if f.zone not in EXCLUDED_ZONES)
    loose_root = sum(1 for p in scope if p["parent"] is root)
    loose_default = sum(1 for p in scope if p["parent"] is not None
                        and p["parent"].ptype in DEFAULT_TYPE_FOLDERS
                        and p["parent"].depth == 1)
    in_inbox = sum(1 for p in scope if p["zone"] == "Received Items")
    outfits = [f for f in folders.values() if f.ptype == "outfit"]
    outfit_links = Counter(p["parent"].id for p in links
                           if p["parent"] is not None and p["parent"].ptype == "outfit")
    folders_with_links = {p["parent"].id for p in links if p["parent"] is not None}
    report["folders"] = {
        "product_like_folders": sum(1 for f in candidates if f.product_like),
        "items_inside_product_like_folders": items_in_products,
        "items_inside_product_like_folders_pct": pct(items_in_products, n_scope),
        "category_like_folders": sum(1 for f in candidates if f.category_like),
        "empty_folders": sum(1 for f in folders.values()
                             if f.zone not in EXCLUDED_ZONES and f.depth and f.depth > 0
                             and not f.children and not f.items
                             and f.id not in folders_with_links),
        "folders_by_direct_item_count": {label: fanout.get(label, 0)
                                         for _, _, label in FANOUT_EDGES},
        "loose_items_in_root": loose_root,
        "loose_items_in_default_type_folders": loose_default,
        "items_in_received_items": in_inbox,
        "outfit_folders": len(outfits),
        "avg_links_per_outfit": round(sum(outfit_links.values()) / len(outfits), 1)
        if outfits else 0,
        "note": ("product-like = 2+ items, 80%+ from one creator, 3 or fewer "
                 "subfolders; category-like = mostly product-like subfolders, "
                 "or 10+ items from 5+ creators"),
    }

    # Names and descriptions ---------------------------------------------
    exact = Counter((p["creator"], p["type"], p["name"]) for p in scope)
    normalised = defaultdict(set)
    norm_groups = Counter()
    for p in scope:
        key = normalise_name(p["name"])
        if key:
            normalised[(p["creator"], p["type"], key)].add(p["name"])
            norm_groups[(p["creator"], p["type"], key)] += 1
    variant_groups = [k for k, v in normalised.items() if len(v) > 1]
    descs = Counter(p["desc"].strip() for p in scope)
    empty_desc = sum(c for d, c in descs.items()
                     if not d or d.lower() == "(no description)")
    boilerplate = sum(c for d, c in descs.items()
                      if d and d.lower() != "(no description)"
                      and c >= BOILERPLATE_MIN_REPEATS)
    report["names"] = {
        "brand_tag_prefix_pct": pct(sum(1 for p in scope if BRAND_PREFIX_RE.match(p["name"])),
                                    n_scope),
        "mentions_mesh_body_name": sum(1 for p in scope if BODY_NAMES_RE.search(p["name"])),
        "objects_that_look_boxed": sum(1 for p in objects if BOXED_RE.search(p["name"])),
        "exact_duplicate_groups": sum(1 for c in exact.values() if c > 1),
        "items_in_exact_duplicate_groups": sum(c for c in exact.values() if c > 1),
        "near_duplicate_groups": sum(1 for c in norm_groups.values() if c > 1),
        "items_in_near_duplicate_groups": sum(c for c in norm_groups.values() if c > 1),
        "variant_groups_differing_only_by_version_body_or_tag": len(variant_groups),
        "empty_description_pct": pct(empty_desc, n_scope),
        "boilerplate_description_pct": pct(boilerplate, n_scope),
        "note": ("duplicates are grouped per creator and type; near-duplicates "
                 "ignore bracketed tags, version numbers and mesh-body names; "
                 "boilerplate = the same description on %d+ items"
                 % BOILERPLATE_MIN_REPEATS),
    }

    # Dates, thumbnails, favorites --------------------------------------
    years = Counter()
    for p in scope:
        if p["created"] > 0:
            try:
                years[str(datetime.datetime.fromtimestamp(
                    p["created"], tz=datetime.timezone.utc).year)] += 1
            except (OverflowError, OSError, ValueError):
                years["invalid"] += 1
    report["dates"] = {"items_by_year_added": dict(sorted(years.items()))}
    report["metadata"] = {
        "items_with_thumbnail": sum(1 for p in scope if p["sd"].get("thumbnail")),
        "folders_with_thumbnail": sum(1 for sd in categories if sd.get("thumbnail")),
        "favorite_items": sum(1 for p in parsed if p["sd"].get("favorite")),
        "favorite_folders": sum(1 for sd in categories if sd.get("favorite")),
    }
    return report


# --------------------------------------------------------------------------
# Output

def print_structure(categories, items, max_depth=3, out=sys.stdout):
    """Folder names (never item names) down to max_depth, with counts, plus
    the folder names that recur anywhere. Unlike the report, this prints
    your folder names: read it before sharing it."""
    folders = {}
    for sd in categories:
        f = Folder(sd)
        if f.id is not None:
            folders[f.id] = f
    infer_unsaved_objects_folder(folders, items)
    root = resolve_folders(folders)
    if root is None:
        out.write("no root folder found\n")
        return
    for f in folders.values():
        parent = folders.get(f.parent)
        if parent is not None:
            parent.children.append(f)
    direct = Counter()
    for sd in items:
        if str(sd.get("type", "")) not in ("link", "link_f"):
            direct[as_uuid(sd.get("parent_id"))] += 1

    totals = {}
    def total(f):
        if f.id not in totals:
            totals[f.id] = direct[f.id] + sum(total(c) for c in f.children)
        return totals[f.id]

    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
    out.write("Folder structure (items: direct / including subfolders)\n")
    out.write("=======================================================\n")

    def walk(f, depth):
        kids = sorted(f.children, key=lambda c: -total(c))
        for c in kids:
            if c.zone in ("Trash", "Current Outfit", ORPHAN_ZONE):
                continue
            label = SYSTEM_FOLDER_LABELS.get(c.ptype, c.name) if c.ptype != "-1" else c.name
            out.write("%s%s  [%d / %d, %d subfolders]\n" % (
                "    " * (depth - 1), label, direct[c.id], total(c), len(c.children)))
            if depth < max_depth and c.ptype not in ("outfit",):
                walk(c, depth + 1)
    walk(root, 1)

    names = Counter(f.name.strip().lower() for f in folders.values()
                    if f.ptype == "-1" and f.depth and f.depth > 0
                    and f.zone not in ("Trash", ORPHAN_ZONE))
    out.write("\nFolder names used 3+ times anywhere\n")
    out.write("-----------------------------------\n")
    for name, count in names.most_common():
        if count < 3:
            break
        out.write("  %-40s %d\n" % (name[:40], count))


def print_report(report, out=sys.stdout):
    def section(title):
        out.write("\n%s\n%s\n" % (title, "-" * len(title)))

    def row(label, value, width=46):
        out.write("  %-*s %s\n" % (width, label, value))

    out.write("Inventory cache report\n======================\n")
    row("cache version", "%d%s" % (report["cache_version"],
        "" if report["cache_version_current"]
        else "  (not a version this script knows; numbers may be off)"))
    if not report.get("has_root_folder"):
        out.write("\nNo root folder found: this does not look like a personal "
                  "inventory cache (the Library cache has no My Inventory root).\n")
        return

    c = report["completeness"]
    section("Completeness")
    row("items whose folder is missing", c["items_with_parent_missing"])
    row("folders not connected to the root", c["folders_not_connected_to_root"])
    row("", c["note"])

    s = report["size"]
    section("Size and shape")
    row("folders", s["folders"])
    row("items (excluding links)", s["items_excluding_links"])
    row("links", s["links"])
    row("items in scope for organising", s["items_in_scope"])
    row("", "(%s)" % s["items_in_scope_note"])
    row("deepest folder level", s["max_folder_depth"])
    row("in-scope items by folder depth",
        ", ".join("%s: %s" % kv for kv in s["items_by_folder_depth"].items()))

    section("Where things are")
    out.write("  %-34s %9s %9s %9s\n" % ("area", "items", "links", "folders"))
    for zone, z in report["zones"].items():
        out.write("  %-34s %9d %9d %9d\n" % (zone[:34], z["items"], z["links"], z["folders"]))

    t = report["types"]
    section("Item types (in scope)")
    for label, count in t["by_type"].items():
        row(label, count)
    row("objects last worn as attachments", t["objects_last_worn_as_attachment"])
    row("coalesced objects", t["objects_coalesced"])
    if t["wearables_by_kind"]:
        row("clothing / body parts by kind",
            ", ".join("%s: %s" % kv for kv in t["wearables_by_kind"].items()))

    p = report["permissions"]
    section("Permissions (yours, in scope)")
    row("no modify", "%d (%.1f%%)" % (p["no_modify"], p["no_modify_pct"]))
    row("no copy", "%d (%.1f%%)" % (p["no_copy"], p["no_copy_pct"]))
    row("no transfer", "%d (%.1f%%)" % (p["no_transfer"], p["no_transfer_pct"]))
    for combo, count in p["combinations"].items():
        row("  " + combo, count)
    row("", p["note"])

    l = report["links"]
    section("Links")
    row("links", l["total"])
    row("folder links", l["folder_links"])
    row("target not in cache", l["target_not_in_cache"])
    for zone, count in l["target_not_in_cache_by_zone"].items():
        row("  in " + zone, count)
    row("", l["note"])

    cr = report["creators"]
    section("Creators (in scope)")
    row("distinct creators", cr["distinct_creators"])
    row("items you created", "%d (%.1f%%)" % (cr["items_you_created"],
                                               cr["items_you_created_pct"]))
    row("share of items from top 10 / 50 / 100",
        "%.1f%% / %.1f%% / %.1f%%" % (cr["top_10_creators_share_pct"],
                                      cr["top_50_creators_share_pct"],
                                      cr["top_100_creators_share_pct"]))
    row("creators with 20+ items", cr["creators_with_20_plus_items"])
    row("creators with a single item", cr["creators_with_1_item"])
    row("items with no creator recorded", cr["items_with_no_creator_recorded"])
    for entry in cr.get("top", []):
        row("  " + entry["creator"][:44], entry["items"])

    f = report["folders"]
    section("Folder patterns")
    row("product-like folders", f["product_like_folders"])
    row("items inside product-like folders", "%d (%.1f%%)" % (
        f["items_inside_product_like_folders"], f["items_inside_product_like_folders_pct"]))
    row("category-like folders", f["category_like_folders"])
    row("empty folders", f["empty_folders"])
    row("folders by direct item count",
        ", ".join("%s: %s" % kv for kv in f["folders_by_direct_item_count"].items()))
    row("loose items in the root", f["loose_items_in_root"])
    row("loose items in Objects/Clothing/etc.", f["loose_items_in_default_type_folders"])
    row("items in Received Items", f["items_in_received_items"])
    row("outfit folders", f["outfit_folders"])
    row("average links per outfit", f["avg_links_per_outfit"])
    row("", f["note"])

    n = report["names"]
    section("Names and descriptions (in scope)")
    row("names starting with a brand tag", "%.1f%%" % n["brand_tag_prefix_pct"])
    row("names mentioning a mesh body", n["mentions_mesh_body_name"])
    row("objects that look boxed", n["objects_that_look_boxed"])
    row("exact duplicate groups (items)", "%d (%d)" % (
        n["exact_duplicate_groups"], n["items_in_exact_duplicate_groups"]))
    row("near-duplicate groups (items)", "%d (%d)" % (
        n["near_duplicate_groups"], n["items_in_near_duplicate_groups"]))
    row("variant groups", n["variant_groups_differing_only_by_version_body_or_tag"])
    row("empty descriptions", "%.1f%%" % n["empty_description_pct"])
    row("boilerplate descriptions", "%.1f%%" % n["boilerplate_description_pct"])
    row("", n["note"])

    section("When items were added (in scope)")
    row("by year", ", ".join("%s: %s" % kv for kv in
                             report["dates"]["items_by_year_added"].items()) or "none")

    m = report["metadata"]
    section("Thumbnails and favorites")
    row("items / folders with a thumbnail", "%d / %d" % (
        m["items_with_thumbnail"], m["folders_with_thumbnail"]))
    row("favorite items / folders", "%d / %d" % (m["favorite_items"], m["favorite_folders"]))


# --------------------------------------------------------------------------
# File discovery

def cache_files_in(folder):
    found = []
    # Some builds keep inventory caches in an inv_cache subfolder.
    for where in (folder, os.path.join(folder, "inv_cache")):
        gz = glob.glob(os.path.join(where, "*.inv.llsd.gz"))
        found += gz + [p for p in glob.glob(os.path.join(where, "*.inv.llsd"))
                       if p + ".gz" not in gz]
        found += glob.glob(os.path.join(where, "hangar-export-*.json"))
    return sorted(found)


def looks_personal(categories):
    ptypes = {str(sd.get("preferred_type")) for sd in categories}
    return "root_inv" in ptypes and "current" in ptypes


def pick_cache_file(folder):
    files = cache_files_in(folder)
    if not files:
        sys.exit("no *.inv.llsd.gz or hangar-export-*.json files in %s" % folder)
    personal = []
    for path in files:
        try:
            _, cats, items = read_cache(path)
        except Exception as exc:  # report and keep looking
            sys.stderr.write("skipping %s: %s\n" % (os.path.basename(path), exc))
            continue
        if looks_personal(cats):
            personal.append((path, len(cats), len(items)))
    if len(personal) == 1:
        sys.stderr.write("using %s\n" % os.path.basename(personal[0][0]))
        return personal[0][0]
    sys.stderr.write("found %d personal inventory caches; pass one of them:\n"
                     % len(personal))
    for path, n_cats, n_items in personal:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        sys.stderr.write("  %s  (%d folders, %d items, saved %s)\n"
                         % (path, n_cats, n_items, mtime.strftime("%Y-%m-%d %H:%M")))
    sys.exit(2)


def find_name_cache(folder):
    for where in (folder, os.path.dirname(folder)):
        found = glob.glob(os.path.join(where, "avatar_name_cache*.xml"))
        if len(found) == 1:
            return found[0]
    return None


# --------------------------------------------------------------------------
# Self-test: a synthetic cache written byte-for-byte the way the viewer
# writes one (LLSDBinaryFormatter::format_impl), so the test does not rely on
# the llsd package's own writer agreeing with its reader.

def _encode_binary_llsd(value):
    if isinstance(value, dict):
        out = [b"{", struct.pack(">I", len(value))]
        for key, val in value.items():
            kb = key.encode("utf-8")
            out += [b"k", struct.pack(">I", len(kb)), kb, _encode_binary_llsd(val)]
        return b"".join(out + [b"}"])
    if isinstance(value, list):
        return b"".join([b"[", struct.pack(">I", len(value))]
                        + [_encode_binary_llsd(v) for v in value] + [b"]"])
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, int):
        return b"i" + struct.pack(">i", value)
    if isinstance(value, uuid.UUID):
        return b"u" + value.bytes
    if isinstance(value, bytes):
        return b"b" + struct.pack(">I", len(value)) + value
    if isinstance(value, str):
        vb = value.encode("utf-8")
        return b"s" + struct.pack(">I", len(vb)) + vb
    if value is None:
        return b"!"
    raise TypeError(type(value))


def _encode_notation_llsd(value):
    """LLSDNotationFormatter's layout, for the older text cache."""
    if isinstance(value, dict):
        return b"{" + b",".join(_encode_notation_llsd(str(k)) + b":" + _encode_notation_llsd(v)
                                for k, v in value.items()) + b"}"
    if isinstance(value, list):
        return b"[" + b",".join(_encode_notation_llsd(v) for v in value) + b"]"
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, int):
        return b"i%d" % value
    if isinstance(value, uuid.UUID):
        return b"u" + str(value).encode("ascii")
    if isinstance(value, bytes):
        return b'b16"' + value.hex().upper().encode("ascii") + b'"'
    if isinstance(value, str):
        return b"'" + value.replace("\\", "\\\\").replace("'", "\\'").encode("utf-8") + b"'"
    if value is None:
        return b"!"
    raise TypeError(type(value))


def synthetic_export():
    """A small hangar-export document, as the plugin writes it (also used by
    the triage self-test). Returns (document, ids)."""
    ids = {k: str(uuid.uuid4()) for k in (
        "me", "brand", "root", "trash", "inbox", "current", "clothing", "dress", "boxes",
        "d1", "d2", "d3", "link", "t1", "unnamed")}
    full = PERM_MODIFY | PERM_COPY | PERM_TRANSFER

    def folder(key, parent, name, kind="None"):
        return {"id": ids[key], "parent": ids[parent] if parent else NULL_ID,
                "name": name, "type": kind}

    def item(key, parent, name, creator, atype="Object", itype="Object", **extra):
        d = {"id": ids[key], "parent": ids[parent], "name": name, "asset_type": atype,
             "inv_type": itype, "creator": ids[creator], "owner": ids["me"], "flags": 0,
             "perms": {"owner": full, "next_owner": PERM_COPY | PERM_TRANSFER},
             "is_link": False}
        d.update(extra)
        return d

    doc = {
        "format": EXPORT_FORMAT, "version": 1, "exported": "2026-10-02T12:00:00+00:00",
        "agent_id": ids["me"], "agent_name": "Test Resident", "root_id": ids["root"],
        "folders": [
            folder("root", None, "My Inventory", "Root"),
            folder("trash", "root", "Trash", "Trash"),
            folder("inbox", "root", "Received Items", "Inbox"),
            folder("current", "root", "Current Outfit", "CurrentOutfit"),
            folder("clothing", "root", "Clothing", "Clothing"),
            folder("dress", "clothing", "[BrandA] Dress"),
            folder("boxes", "root", "Plain folder")],
        "items": [
            item("d1", "dress", "[BrandA] Dress - Maitreya", "brand", atype="Clothing",
                 itype="Wearable", wearable_type="Shirt", flags=4),
            item("d2", "dress", "[BrandA] Dress - Legacy", "brand", itype="Attachment"),
            item("d3", "dress", "[BrandA] Dress texture", "brand", atype="Texture", itype="Texture"),
            item("link", "boxes", "link", "me", atype="Link", itype="Object",
                 is_link=True, link_target=ids["d1"]),
            item("t1", "trash", "Old thing", "brand"),
            item("unnamed", "boxes", "From a deleted account", "unnamed")],
        "creators": {ids["brand"]: "Brand A (Brandy)"}}
    return doc, ids


def _self_test():
    import tempfile

    me, brand_a, brand_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = defaultdict(uuid.uuid4)
    FULL = PERM_MODIFY | PERM_COPY | PERM_TRANSFER

    def cat(key, parent, ptype, name):
        return {"cat_id": ids[key], "parent_id": ids[parent] if parent else uuid.UUID(int=0),
                "type": "category", "preferred_type": ptype, "name": name,
                "owner_id": me, "version": 3}

    def item(key, parent, name, creator, atype="object", mask=FULL, **extra):
        sd = {"item_id": ids[key], "parent_id": ids[parent],
              "permissions": {"creator_id": creator, "owner_id": me,
                              "last_owner_id": creator, "group_id": uuid.UUID(int=0),
                              "is_owner_group": False, "base_mask": mask,
                              "owner_mask": mask, "group_mask": 0,
                              "everyone_mask": 0, "next_owner_mask": mask},
              "asset_id": uuid.uuid4(), "type": atype, "inv_type": "object",
              "flags": struct.pack(">I", 0), "sale_info": {"sale_type": "not", "sale_price": 10},
              "name": name, "desc": "", "created_at": 1700000000}
        sd.update(extra)
        return sd

    def link(key, parent, target, shadow=False):
        sd = item(key, parent, "link", me, atype="link")
        if shadow:
            del sd["asset_id"]
            sd["shadow_id"] = uuid.UUID(bytes=bytes(
                a ^ b for a, b in zip(ids[target].bytes, MAGIC_ID.bytes)))
        else:
            sd["asset_id"] = ids[target]
        return sd

    categories = [
        cat("root", None, "root_inv", "My Inventory"),
        cat("trash", "root", "trash", "Trash"),
        cat("cof", "root", "current", "Current Outfit"),
        cat("outfits", "root", "my_otfts", "My Outfits"),
        cat("outfit1", "outfits", "outfit", "Party"),
        cat("objects", "root", "object", "Objects"),
        cat("inbox", "root", "inbox", "Received Items"),
        cat("rlv", "root", "-1", "#RLV"),
        cat("clothes", "root", "-1", "Clothes"),
        cat("dress", "clothes", "-1", "[BrandA] Dress"),
        cat("boots", "clothes", "-1", "[BrandB] Boots"),
        cat("gloves", "clothes", "-1", "[BrandA] Gloves"),
        cat("empty", "root", "-1", "New Folder"),
    ]
    items = [
        item("d1", "dress", "[BrandA] Dress - Maitreya v1.2", brand_a, inv_type="attach"),
        item("d2", "dress", "[BrandA] Dress - Legacy v1.2", brand_a, inv_type="attach"),
        item("d3", "dress", "[BrandA] Dress HUD", brand_a),
        item("b1", "boots", "[BrandB] Boots", brand_b, mask=PERM_COPY | PERM_TRANSFER),
        item("b2", "boots", "[BrandB] Boots", brand_b),
        item("g1", "gloves", "[BrandA] Gloves", brand_a),
        item("g2", "gloves", "[BrandA] Gloves texture", brand_a, atype="texture"),
        item("o1", "objects", "Unpack Me Box", brand_b),
        item("o2", "objects", "My build", me),
        item("r1", "inbox", "Gift", brand_b),
        item("t1", "trash", "Old thing", brand_b),
        item("x1", "gone", "Orphan", brand_b),
        link("l1", "cof", "d1"),
        link("l2", "outfit1", "d1", shadow=True),
        link("l3", "outfit1", "deleted"),
    ]
    blob = struct.pack(">I", EXPECTED_CACHE_VERSION) + _encode_binary_llsd(
        {"categories": categories, "items": items}) + b"\n"
    name_xml = llsd.format_xml({"agents": {
        str(brand_a): {"username": "brand.a", "display_name": "Brand A",
                       "legacy_first_name": "Brand", "legacy_last_name": "A"}}})

    # The older text layout, as the released build on Windows writes it.
    text = b"\r\n".join([_encode_notation_llsd({"inv_cache_version": 3})]
                         + [_encode_notation_llsd(sd) for sd in categories + items]) + b"\r\n"

    reports = []
    with tempfile.TemporaryDirectory() as tmp:
        with gzip.open(os.path.join(tmp, "%s.inv.llsd.gz" % me), "wb") as f:
            f.write(blob)
        with open(os.path.join(tmp, "avatar_name_cache.xml"), "wb") as f:
            f.write(name_xml)
        path = pick_cache_file(tmp)
        names = read_name_cache(find_name_cache(tmp))
        reports.append(("binary v5", analyse(*read_cache(path), names=names, top_creators=2)))

        old = os.path.join(tmp, "old")
        os.makedirs(os.path.join(old, "inv_cache"))
        with gzip.open(os.path.join(old, "inv_cache", "%s.inv.llsd.gz" % me), "wb") as f:
            f.write(text)
        with open(os.path.join(old, "avatar_name_cache.xml"), "wb") as f:
            f.write(name_xml)
        path = pick_cache_file(old)
        names = read_name_cache(find_name_cache(os.path.dirname(path)))
        reports.append(("text v3", analyse(*read_cache(path), names=names, top_creators=2)))

    # Older viewers can leave out the Objects folder's own record while
    # saving everything inside it; the report must reattach its contents.
    no_objects = [c for c in categories if c["cat_id"] != ids["objects"]]
    inferred = analyse(EXPECTED_CACHE_VERSION, no_objects,
                       [sd for sd in items if sd["item_id"] != ids["x1"]])
    total = 0
    for got, want in ((inferred["completeness"]["objects_folder_inferred"], True),
                      (inferred["completeness"]["folders_not_connected_to_root"], 0),
                      (inferred["zones"]["Objects"]["items"], 2),
                      (inferred["completeness"]["items_with_parent_missing"], 0),
                      (inferred["size"]["items_in_scope"], 10)):
        if got != want:
            sys.exit("self-test FAILED on the inferred Objects folder: got %r, want %r"
                     % (got, want))
        total += 1

    total += _check_export_reader()
    for layout, report in reports:
        total += _check_self_test_report(layout, report)
    print_report(reports[0][1])
    print("\nself-test passed (%d checks over %d layouts)" % (total, len(reports)))


def _check_export_reader():
    import tempfile

    doc, ids = synthetic_export()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hangar-export-Test_Resident-20261002-120000.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        meta, cats, items = read_cache(path)
        picked = pick_cache_file(tmp)
        # The same bytes under another name are still recognised as an export.
        other = os.path.join(tmp, "x.dat")
        with open(path, "rb") as src, open(other, "wb") as dst:
            dst.write(src.read())
        sniffed = read_cache(other)[1:]
    kinds = {str(c["cat_id"]): c["preferred_type"] for c in cats}
    by_id = {str(i["item_id"]): i for i in items}
    link = by_id[ids["link"]]
    checks = [
        ("export: path picked from a folder", picked, path),
        ("export: meta is a current cache version", (int(meta), meta.agent_name), (EXPECTED_CACHE_VERSION, "Test Resident")),
        ("export: sniffed without .json", len(sniffed[0]), 7),
        ("export: folder count", len(cats), 7),
        ("export: item count", len(items), 6),
        ("export: folder kinds", [kinds[ids[k]] for k in ("root", "trash", "inbox", "current", "clothing", "dress")],
         ["root_inv", "trash", "inbox", "current", "clothing", "-1"]),
        ("export: root has the null parent", str(cats[0]["parent_id"]), NULL_ID),
        ("export: item types", [by_id[ids[k]]["type"] for k in ("d1", "d3", "link")], ["clothing", "texture", "link"]),
        ("export: inv types", [by_id[ids[k]]["inv_type"] for k in ("d1", "d2", "d3")], ["wearable", "attach", "texture"]),
        ("export: creator id kept in permissions",
         str(by_id[ids["d1"]]["permissions"]["creator_id"]), ids["brand"]),
        ("export: masks kept", (by_id[ids["d1"]]["permissions"]["owner_mask"],
                                by_id[ids["d1"]]["permissions"]["next_owner_mask"]),
         (PERM_MODIFY | PERM_COPY | PERM_TRANSFER, PERM_COPY | PERM_TRANSFER)),
        ("export: flags are ints the report understands", item_flags(by_id[ids["d1"]]), 4),
        ("export: link target in asset_id", str(link_target(link)), ids["d1"]),
        ("export: creator names", meta.creators, {ids["brand"]: "Brand A (Brandy)"}),
        ("export: unresolved creator absent", ids["unnamed"] in meta.creators, False),
    ]
    failed = [(label, got, want) for label, got, want in checks if got != want]
    for label, got, want in failed:
        sys.stderr.write("%s: got %r, want %r\n" % (label, got, want))
    if failed:
        sys.exit("self-test FAILED on the export reader (%d of %d checks)" % (len(failed), len(checks)))
    report = analyse(meta, cats, items)
    if report["size"]["items_in_scope"] != 4:
        sys.exit("self-test FAILED: export analysed to %r items in scope, want 4"
                 % report["size"]["items_in_scope"])
    return len(checks) + 1


def _check_self_test_report(layout, report):
    checks = [
        (report["cache_version_current"], True),
        (report["size"]["folders"], 13),
        (report["size"]["items_excluding_links"], 12),
        (report["size"]["links"], 3),
        # 12 real items minus trash and the orphan
        (report["size"]["items_in_scope"], 10),
        (report["size"]["max_folder_depth"], 2),
        (report["completeness"]["items_with_parent_missing"], 1),
        (report["zones"]["Trash"]["items"], 1),
        (report["zones"]["#RLV"]["folders"], 1),
        (report["permissions"]["no_modify"], 1),
        (report["links"]["target_not_in_cache"], 1),
        (report["links"]["target_not_in_cache_by_zone"], {"My Outfits": 1}),
        (report["creators"]["distinct_creators"], 3),
        (report["creators"]["items_you_created"], 1),
        (report["creators"]["top"][0], {"creator": "Brand A (brand.a)", "items": 5}),
        (report["folders"]["product_like_folders"], 3),
        (report["folders"]["items_inside_product_like_folders"], 7),
        (report["folders"]["category_like_folders"], 1),
        # "New Folder" and the empty #RLV folder
        (report["folders"]["empty_folders"], 2),
        (report["folders"]["loose_items_in_default_type_folders"], 2),
        (report["folders"]["items_in_received_items"], 1),
        (report["folders"]["avg_links_per_outfit"], 2.0),
        (report["types"]["objects_last_worn_as_attachment"], 2),
        (report["names"]["objects_that_look_boxed"], 1),
        (report["names"]["mentions_mesh_body_name"], 2),
        (report["names"]["exact_duplicate_groups"], 1),
        # "Dress - Maitreya v1.2" and "Dress - Legacy v1.2" collapse together
        (report["names"]["variant_groups_differing_only_by_version_body_or_tag"], 1),
        (report["names"]["brand_tag_prefix_pct"], 70.0),
    ]
    failed = [(i, got, want) for i, (got, want) in enumerate(checks) if got != want]
    for i, got, want in failed:
        sys.stderr.write("%s check %d: got %r, want %r\n" % (layout, i, got, want))
    if failed:
        sys.exit("self-test FAILED on the %s layout (%d of %d checks)"
                 % (layout, len(failed), len(checks)))
    return len(checks)


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Read-only summary of a viewer inventory cache file.")
    parser.add_argument("path", nargs="?",
                        help="an <uuid>.inv.llsd.gz file, or the cache folder holding it")
    parser.add_argument("--names", help="avatar_name_cache.xml, for --top-creators "
                        "(default: found next to the inventory file)")
    parser.add_argument("--top-creators", type=int, default=0, metavar="N",
                        help="also list the N creators with most items, by name "
                        "(off by default: this is the only option that prints names)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--structure", type=int, nargs="?", const=3, metavar="DEPTH",
                        help="instead of the report, print folder NAMES (not item names) "
                        "down to DEPTH levels (default 3) with item counts. Contains "
                        "your folder names: check it before sharing")
    parser.add_argument("--self-test", action="store_true",
                        help="run against a synthetic cache and check the numbers")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return
    if not args.path:
        parser.error("give the inventory cache file or folder (or --self-test)")

    path = args.path
    if os.path.isdir(path):
        path = pick_cache_file(path)
    names = None
    if args.top_creators:
        names_path = args.names or find_name_cache(os.path.dirname(os.path.abspath(path)))
        names = read_name_cache(names_path) if names_path else {}

    try:
        version, categories, items = read_cache(path)
    except (OSError, ValueError, EOFError, llsd.LLSDParseError) as exc:
        sys.exit("could not read %s as an inventory cache: %s" % (path, exc))
    if args.top_creators and not names and getattr(version, "creators", None):
        names = version.creators
    if args.structure:
        print_structure(categories, items, max_depth=args.structure)
        return
    report = analyse(version, categories, items, names=names, top_creators=args.top_creators)
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_report(report)


if __name__ == "__main__":
    main()
