#!/usr/bin/env python3
"""\
@file   inventory_triage.py
@brief  Preview-only inventory triage: groups unfiled product folders by
        brand, suggests where each brand belongs from how the inventory is
        already organised, records one decision per brand, and writes the
        resulting plan of folder moves. It never moves anything.

        Design, in short:
          * the unit is a product folder (a vendor delivery), never split
            or renamed;
          * the layout is Category > Brand > Product set, as already used;
          * unfiled product folders (in the root, Received Items, and
            "Need to Sort" / "To Unpack" style folders) are grouped by brand;
          * a brand that already has a folder is filed there automatically;
          * every other brand gets a suggested destination, learned from
            where similar things already live, and you confirm or change
            it once for the whole brand;
          * decisions are saved to a map file, so a rerun resumes, and
            future deliveries of a known brand file themselves;
          * #RLV, RLV staging folders, AO structures, outfits, Current
            Outfit, Trash and Marketplace are never touched.

        It reads the viewer's inventory cache (see inventory_cache_report.py
        for where to find it), which only a logged-out viewer writes. The
        plan is a proposal against that snapshot: whatever applies it must
        re-check against the live inventory first.

        Usage:
            python tools/inventory/inventory_triage.py <cache> --list 30
            python tools/inventory/inventory_triage.py <cache>            (interactive)
            python tools/inventory/inventory_triage.py <cache> --plan plan.csv
            python tools/inventory/inventory_triage.py --self-test
            python tools/inventory/inventory_triage.py prune <export.json> <plan.csv>
            python tools/inventory/inventory_triage.py docs <export.json> <plan.csv>
            python tools/inventory/inventory_triage.py dedupe <export.json> <review.tsv> <plan.csv>
            python tools/inventory/inventory_triage.py rlv <export.json> <plan.csv> <review.tsv> [map.json]

        prune, docs and dedupe write an action plan of item and
        folder actions from a Hangar export, as drafts for review.

        The decision map defaults to triage_map.json in the current folder
        (--map to change it). Needs inventory_cache_report.py alongside it
        and the llsd package.

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
import csv
import datetime
import json
import math
import os
import random
import re
import sys
from collections import Counter, defaultdict
from urllib.parse import quote_plus

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory_cache_report as icr  # noqa: E402

if not hasattr(icr, "infer_unsaved_objects_folder"):
    sys.exit("inventory_cache_report.py next to this script is an older copy; "
             "replace it with the current one (it must be the same version as this script)")


SEP = " > "

# Areas that are never a source or a destination.
EXCLUDED_PTYPES = {"trash", "current", "my_otfts", "outfit", "merchant", "outbox",
                   "stock", "version", "animover", "lstndfnd", "rlv", "favorite",
                   "callcard", "landmark", "inbox"}
EXCLUDED_NAME_RE = re.compile(
    r"(^|[^a-z])rlv([^a-z]|$)|wardrobe import|fitting required|needs? fitting|redelivery",
    re.IGNORECASE)

# Folders that hold unfiled things. Their product folders are sources.
STAGING_NAME_RE = re.compile(
    r"^[\W_]*(need(s)? to sort|to sort|unsorted|to unpack|unpacking|"
    r"\[?organi[sz]ation\]?|sorting|import|opened|new folder)[\W_]*$",
    re.IGNORECASE)

# A product folder has items and at most one level of part subfolders.
PRODUCT_MAX_PARTS = 6
# Subfolder names that mark parts of one product, never products of a brand
# ("Marigold > Delilah > Hair / HUDs / Docs").
PART_NAME_RE = re.compile(
    r"^[\W_]*(hair|huds?|docs?|documents?|notecards?|scripts?|textures?|alphas?|appliers?|"
    r"extras?|demos?|backups?|shoes|clothing|clothes|materials?|animations?|poses?|sounds?|"
    r"gestures?|fitted|rigged|unrigged|bom|options?|addons?|add-?ons?|readme|"
    r"instructions?|info|landmarks?|objects?|parts?)[\W_]*$",
    re.IGNORECASE)
# Where still-boxed purchases wait, grouped by brand, whichever way they
# arrived (Marketplace deliveries land in Received Items, in-world ones in
# Objects).
TO_UNPACK = "Objects > To Unpack"
# A brand with at least this many unpacked products is split by type.
TYPE_SPLIT_MIN = 6

BOX_NAME_RE = re.compile(
    r"\(\s*box(ed)?\s*\)|\bboxed\b|\[\s*box(ed)?\s*\]|\b(wear|rez|add|touch|click)"
    r"( me)? to unpack\b|\bunpacker\b|\bcrated\b|\(\s*box\b", re.IGNORECASE)
UNPACKED_NAME_RE = re.compile(r"\bunpacked\b", re.IGNORECASE)
# A folder named like this is an area for boxed things: a brand folder in it
# (or under it) is a boxed home, not a home for that brand's unpacked products.
# Whole words, so Toolbox and Boxer are not hit.
BOXED_AREA_RE = re.compile(
    r"\b(boxed|boxes|to unpack|unpack(ed)? later|packaged)\b", re.IGNORECASE)

# Product types, first match wins; checked against folder and item names.
# Rooms come before buildings, since sets are often named after a style.
PRODUCT_TYPES = [
    ("Bedroom", r"bed(room)?\b|nursery|crib|wardrobe|dresser|nightstand"),
    ("Living Room", r"living|parlou?r|sofa|couch|lounge|armchair|fireplace"),
    ("Kitchen & Dining", r"kitchen|dining|cooker|oven|fridge|pantry|\bbar\b|tea set|cafe"),
    ("Bathroom", r"bath(room)?|shower|toilet|vanity"),
    ("Office & Study", r"office|desk|study|reading|library|bookcase|books"),
    # After the rooms: "Cottage Bedroom" is a bedroom, "Loft Kitchen" a kitchen.
    ("Skyboxes & Buildings", r"skybox|house|cottage|lodge|cabin|townhouse|manor|loft|"
                             r"apartment|studio\b|building|shop\b|store\b|barn|treehouse|den\b"),
    ("Garden & Outdoor", r"garden|patio|outdoor|hedge|gate|fence|plant|tree|flower|pool|"
                         r"chess|picnic|porch"),
    ("Lighting", r"lamp|light|candle|lantern|chandelier"),
    ("Decor", r"rug|painting|art|vase|clock|mirror|clutter|decor|plaster|frame|pillow"),
    ("Gifts & Gacha", r"gacha|gift|freebie|advent"),
    ("Dresses", r"dress|gown"),
    ("Tops", r"top|tee|shirt|sweater|cardigan|jacket|hoodie|blouse|corset|bodysuit"),
    ("Bottoms", r"pants|jeans|skirt|shorts|leggings|trousers"),
    ("Lingerie & Swim", r"lingerie|bra\b|panties|thong|bikini|swim|stockings|garter"),
    ("Shoes", r"shoes|boots|heels|sneakers|sandals|slippers"),
    ("Accessories", r"necklace|earrings|ring|bracelet|choker|glasses|bag|hat|cap|collar|cuffs"),
]
PRODUCT_TYPE_RES = [(label, re.compile(r"\b(" + rx + r")", re.IGNORECASE))
                    for label, rx in PRODUCT_TYPES]


# Contents-based box classifier (see classify_box). An object named like a
# box, crate or unpacker.
BOXY_ITEM_RE = re.compile(
    r"\b(box|boxed|bxd|crate|crated|unpack|unpacker|unpackme|rez me|rezz me)\b"
    r"|\(box\)|\[box\]", re.IGNORECASE)
# Packaging words to strip from a folder name before comparing it with its one object.
PACKAGING_RE = re.compile(
    r"\b(box|boxed|bxd|bag|crate|crated|unpack|unpacker|wear to unpack|add to unpack|"
    r"rez to unpack|wear me|add me|rez me|rez & touch|rezz|add/rezz|hud unpacker|unpacker hud)\b"
    r"|[\(\[\{]\s*(box|boxed|bxd|unpack\w*)\s*[\)\]\}]", re.IGNORECASE)
BOX_NAMED_RE = re.compile(
    r"\b(bag|box|boxed|bxd)\b|\bto unpack\b|\bunpacker\b|\bcrated\b", re.IGNORECASE)
# An object that ends in an attachment point is worn, never a box: "Q&Q-posture V2.03 (chin)".
ATTACHED_RE = re.compile(
    r"\((l |r |left |right )?(upper |lower )?(arm|forearm|leg|hand|foot|hip|pec|chest|neck|chin|nose|"
    r"skull|mouth|spine|stomach|pelvis|shoulder|ear|eye|eyeball|jaw|lip|tongue|tail|wing|groin|"
    r"top left|top right|top center|bottom left|bottom right|bottom|center|top|avatar center)\)\s*$",
    re.IGNORECASE)
# Only the vendor's leftovers: a landmark, an ad texture, a notecard. Nothing to file.
HUSK_TYPES = {"landmark", "texture", "notecard", "snapshot"}


def _loose(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def classify_box(inv, f):
    """What a folder holds, from its contents rather than its name: (kind, why).

    BOXED: still a box. UNPACKED: the product's own parts. NOTBOX: nothing to
    unpack (textures, settings, notecards only). SINGLE: one plain object (a
    rezzer or a box, no evidence either way). AMBIG: unclear (two plain objects
    in a box-named Marketplace delivery, one object plus wearables). HUSK: only
    the vendor's landmark, ad texture or notecard is left. Vendors name opened
    folders "(boxed)" and boxes "Set", so the name only breaks ties."""
    cached = inv._box_kind.get(f.id)
    if cached is not None:
        return cached
    inv._box_kind[f.id] = result = _classify_box(inv, f)
    return result


def _classify_box(inv, f):
    items = inv.items[f.id]
    parent = inv.folders.get(f.parent)
    from_inbox = parent is not None and parent.ptype == "inbox"
    if f.children:
        return "UNPACKED", "has subfolders"
    if UNPACKED_NAME_RE.search(f.name):
        return "UNPACKED", "name says unpacked"
    objs = [n for ty, _, n in items if ty == "object"]
    wear = sum(1 for ty, _, _ in items if ty in ("clothing", "bodypart"))
    boxy = [n for n in objs if BOXY_ITEM_RE.search(n)]
    name_boxed = bool(BOX_NAMED_RE.search(f.name))
    if wear >= 3:
        return "UNPACKED", "wearables present"
    if wear and len(objs) >= 2:
        return "UNPACKED", "wearables present"
    if not objs:
        kinds = {ty for ty, _, _ in items}
        if kinds <= HUSK_TYPES and sum(1 for ty, _, _ in items if ty == "texture") <= 1:
            return "HUSK", "only the vendor's landmark/ad/notecard left"
        return "NOTBOX", "no objects"
    if len(objs) == 1 and ATTACHED_RE.search(objs[0]):
        return "UNPACKED", "one worn object (attachment point in its name)"
    if len(objs) >= 3:
        if len(objs) - len(boxy) <= 1:
            return "BOXED", "%d boxes" % len(boxy)
        return "UNPACKED", "%d objects, %d boxy" % (len(objs), len(boxy))
    if len(objs) == 2:
        if boxy:
            return "BOXED", "2 objects, one a box"
        if from_inbox and name_boxed:
            return "AMBIG", "2 plain objects in a box-named delivery"
        return "UNPACKED", "2 plain objects"
    o = objs[0]
    if BOXY_ITEM_RE.search(o):
        return "BOXED", "one box object"
    if name_boxed:
        if from_inbox:
            return "BOXED", "one object in a box-named delivery"
        stripped, on = _loose(PACKAGING_RE.sub(" ", f.name)), _loose(o)
        if on and stripped and (on == stripped or on.startswith(stripped) or stripped.startswith(on)):
            return "UNPACKED", "opened box: the one object is the product itself"
        return "BOXED", "one object, folder says box"
    if wear:
        return "AMBIG", "one object plus wearables"
    return "SINGLE", "one plain object"


def is_boxed(inv, f):
    """Still packed. The contents decide when they are clear (BOXED, UNPACKED);
    otherwise the name does: named as a box or unpacker, or a lone object named so."""
    kind = classify_box(inv, f)[0]
    if kind in ("BOXED", "UNPACKED"):
        return kind == "BOXED"
    if UNPACKED_NAME_RE.search(f.name):
        return False
    if BOX_NAME_RE.search(f.name):
        return True
    items = inv.items[f.id]
    return (not f.children and len(items) == 1 and items[0][0] == "object"
            and bool(BOX_NAME_RE.search(items[0][2]) or icr.BOXED_RE.search(items[0][2])))


TEXTURE_TYPES = {"texture", "material"}
TEXTURE_AREAS = ["Materials", "Textures"]


def is_textures(inv, f):
    """A product folder holding only textures and materials (a vendor's
    normal maps, PBR sets): it belongs with the textures, not the brand's
    clothing or furniture."""
    types = inv.types(f)
    return bool(types) and set(types) <= TEXTURE_TYPES


def product_type(inv, f):
    """The type of a product from its folder name, else its item names."""
    for text in [f.name] + [n for _, _, n in inv.items[f.id]]:
        for label, rx in PRODUCT_TYPE_RES:
            if rx.search(text):
                return label
    return None


# How many of the most similar filed brands vote on a new brand's home.
SIMILAR_BRANDS = 12
# Below this word similarity to every filed brand, a suggestion is a guess.
WEAK_WORD_MATCH = 0.05
# Three or more creators with two or more items each means a mixed folder.
MIXED_MIN_CREATORS = 3

# Words that say nothing about what a product is.
STOP_WORDS = set("""
a an and the of for with in on to by from x v
set sets pack fatpack fullpack full fullset box boxed unpacked unpack unpacker
add addme me wear rez open hud edition version update new gift group exclusive
colors colours all pbr
""".split())

WORD_RE = re.compile(r"[a-z][a-z0-9]+")
# Brand keys keep one-letter words, so "R&D" is "rd" rather than nothing.
KEY_RE = re.compile(r"[a-z0-9]+")
SEPARATORS_RE = re.compile(
    r"\s+[-~|/\\:]+\s+|\s*//\s*|\s*::\s*|\.\s+|:\s*|\s+\|\s+|>>+|<<+")
# "M O C H I" -> "MOCHI"
SPACED_LETTERS_RE = re.compile(r"\b(?:(?:[A-Z] ){2,}[A-Z]|(?:[a-z] ){2,}[a-z])\b")
# A name that is only a word like these carries no brand.
NOT_BRANDS = set("the a an all my new temp demo unpack example prim big little high mystery".split())
BRACKET_TAG_RE = re.compile(r"^\s*[\[\{\(<]\s*([^\]\}\)>]{1,40}?)\s*[\]\}\)>]")
SYMBOL_TAG_RE = re.compile(r"^\s*([+*:.~=|•♥★]+)\s*([^+*:~=|•♥★]{1,40}?)\s*[+*:.~=|•♥★]+")


def norm(text):
    return " ".join(KEY_RE.findall(text.lower().replace("*", " ")))


def squash(text):
    return norm(text).replace(" ", "")


def brand_key_text(name):
    """The brand part of a vendor folder name, in its original case, or ""
    when the name carries no recognisable brand."""
    s = SPACED_LETTERS_RE.sub(lambda m: m.group(0).replace(" ", ""), name).strip()
    lead = re.sub(r"^[^\w\[\{\(<]+", "", s)     # "::{u.f.o}::" -> "{u.f.o}::"
    m = BRACKET_TAG_RE.match(lead) or SYMBOL_TAG_RE.match(s)
    key = ""
    if m:
        tag = m.group(m.lastindex).strip()
        if norm(tag) and norm(tag) not in STOP_WORDS:
            key = tag
    if not key:
        rest = re.sub(r"^[^\w]+", "", s)
        first = SEPARATORS_RE.split(rest, maxsplit=1)[0].strip()
        words = first.split()
        if not words:
            return ""
        key = first if len(words) <= 2 else words[0]
    key = re.sub(r"[{}\[\]<>()*]", "", key)
    key = re.sub(r"^[^\w]+|[^\w!'.]+$", "", key).strip()
    if len(norm(key).replace(" ", "")) < 2 or key.isdigit() or norm(key) in NOT_BRANDS:
        return ""
    return key


# --------------------------------------------------------------------------
# The inventory, as folders with the facts triage needs

class Inventory(object):
    def __init__(self, categories, items, creators=None):
        # creator UUID (lower case) -> "Legacy Name (display name)", where known
        # (a hangar-export file carries these; a viewer cache does not).
        self.creator_names = dict(creators or {})
        self.folders = {}
        for sd in categories:
            f = icr.Folder(sd)
            if f.id is not None:
                self.folders[f.id] = f
        icr.infer_unsaved_objects_folder(self.folders, items)
        self.root = icr.resolve_folders(self.folders)
        if self.root is None:
            raise ValueError("no My Inventory root folder: not a personal inventory cache")
        for f in self.folders.values():
            parent = self.folders.get(f.parent)
            if parent is not None:
                parent.children.append(f)
        self._filed_boxes = {}
        self._box_kind = {}
        self.items = defaultdict(list)
        # The same items with their ids: folder id -> [{id, name, type, creator}]
        # (what an item-level plan needs; links are left out like above).
        self.item_rows = defaultdict(list)
        # Folders holding a link: not empty on the server, though links are left out above.
        self.link_parents = set()
        for sd in items:
            if str(sd.get("type", "")) in ("link", "link_f"):
                self.link_parents.add(icr.as_uuid(sd.get("parent_id")))
                continue
            perms = sd.get("permissions") or {}
            creator = str(icr.as_uuid(perms.get("creator_id")) or icr.NULL_ID)
            parent_id = icr.as_uuid(sd.get("parent_id"))
            self.items[parent_id].append(
                (str(sd.get("type", "")), creator, sd.get("name", "") or ""))
            self.item_rows[parent_id].append(
                {"id": str(icr.as_uuid(sd.get("item_id")) or ""), "name": sd.get("name", "") or "",
                 "type": str(sd.get("type", "")), "creator": creator})
        self._height, self._count, self._creators, self._types = {}, {}, {}, {}
        self.kind = {}
        self._classify()

    # Subtree facts ----------------------------------------------------------
    def height(self, f):
        if f.id not in self._height:
            self._height[f.id] = 1 + max((self.height(c) for c in f.children), default=-1)
        return self._height[f.id]

    def count(self, f):
        if f.id not in self._count:
            self._count[f.id] = len(self.items[f.id]) + sum(self.count(c) for c in f.children)
        return self._count[f.id]

    def creators(self, f):
        if f.id not in self._creators:
            acc = Counter(c for _, c, _ in self.items[f.id] if c != icr.NULL_ID)
            for c in f.children:
                acc.update(self.creators(c))
            self._creators[f.id] = acc
        return self._creators[f.id]

    def types(self, f):
        if f.id not in self._types:
            acc = Counter(t for t, _, _ in self.items[f.id])
            for c in f.children:
                acc.update(self.types(c))
            self._types[f.id] = acc
        return self._types[f.id]

    def dominant_creator(self, f, share=0.6):
        acc = self.creators(f)
        total = sum(acc.values())
        if not total:
            return None
        creator, n = acc.most_common(1)[0]
        return creator if n >= share * total else None

    def path(self, f):
        parts = []
        while f is not None and f is not self.root:
            parts.append(self.label(f))
            f = self.folders.get(f.parent)
        return SEP.join(reversed(parts))

    def label(self, f):
        if f.ptype != "-1" and f.ptype in icr.SYSTEM_FOLDER_LABELS:
            return icr.SYSTEM_FOLDER_LABELS[f.ptype]
        return f.name

    def ancestors(self, f):
        cur = self.folders.get(f.parent)
        while cur is not None and cur is not self.root:
            yield cur
            cur = self.folders.get(cur.parent)

    # Classification -----------------------------------------------------------
    def excluded(self, f):
        if f.ptype in EXCLUDED_PTYPES and f.ptype != "inbox":
            return True
        if f.zone in ("Trash", "Current Outfit", "My Outfits", "Marketplace Listings",
                      "Merchant Outbox", "Animation Overrides", "#RLV",
                      icr.ORPHAN_ZONE):
            return True
        return any(EXCLUDED_NAME_RE.search(a.name) for a in [f] + list(self.ancestors(f)))

    def staging(self, f):
        """Inside Received Items or a to-sort / to-unpack folder."""
        chain = [f] + list(self.ancestors(f))
        return self.opened_in_to_unpack(f) or any(
            a.ptype == "inbox" or
            (STAGING_NAME_RE.search(a.name) and not self.is_boxed_destination(a))
            for a in chain)

    def is_boxed_destination(self, f):
        """The To Unpack destination folder itself, or a folder inside it: where
        boxed things are already filed, never a source. Except a product folder
        in there whose contents are plainly opened parts (classify_box): that is
        an unpacked product filed in the wrong place, so it is a source again."""
        if f.id not in self._filed_boxes:
            path = self.path(f)
            inside = path == TO_UNPACK or path.startswith(TO_UNPACK + SEP)
            self._filed_boxes[f.id] = inside and not self.opened_in_to_unpack(f)
        return self._filed_boxes[f.id]

    def opened_in_to_unpack(self, f):
        """A leaf product folder under To Unpack that classify_box says is opened
        (leaf: a brand folder holding one box is a "product" with a subfolder
        too, and "has subfolders" says nothing about whether it is opened)."""
        return (self.kind.get(f.id) == "product" and not f.children
                and self.path(f).startswith(TO_UNPACK + SEP)
                and classify_box(self, f)[0] == "UNPACKED")

    def _is_product(self, f):
        if f.ptype != "-1" or f is self.root or self.count(f) == 0:
            return False
        if STAGING_NAME_RE.match(f.name):
            return False
        h = self.height(f)
        if h > 2 or (h >= 1 and len(f.children) > PRODUCT_MAX_PARTS):
            return False
        if h == 2:
            # A product whose parts have a part of their own (Shoes > Backup):
            # still one product if it is small all the way down and nothing
            # in it is named like a product of a brand.
            if any(self.height(c) > 1 or len(c.children) > 2 for c in f.children):
                return False
            if self._children_named_as_products(f):
                return False
        if f.children and all(PART_NAME_RE.match(c.name) for c in f.children):
            # Every subfolder is a generic part (Hair, HUDs, Docs): one product,
            # however many parts and wherever its items sit.
            many = [c for c, n in self.creators(f).items() if n >= 2]
            return len(many) < MIXED_MIN_CREATORS
        if h == 1 and not self.items[f.id] and (len(f.children) > 2
                                                or self._children_named_as_products(f)):
            return False
        many = [c for c, n in self.creators(f).items() if n >= 2]
        return len(many) < MIXED_MIN_CREATORS

    def _children_named_as_products(self, f):
        """Children named like products of one brand ("BIRCH - Sonya Set")
        rather than like parts of one product ("HUD", "Alphas")."""
        kids = f.children
        if len(kids) < 2:
            return False
        own = squash(f.name)
        if own and sum(1 for c in kids if squash(c.name).startswith(own)) >= 0.5 * len(kids):
            return True
        keys = Counter(squash(brand_key_text(c.name)) for c in kids)
        key, n = keys.most_common(1)[0]
        return len(key) >= 3 and n >= 2 and n >= 0.5 * len(kids)

    def _classify(self):
        for f in self.folders.values():
            if self._is_product(f):
                parent = self.folders.get(f.parent)
                self.kind[f.id] = "part" if parent is not None and self._is_product(parent) \
                    else "product"
        for f in self.folders.values():
            if f.id in self.kind:
                continue
            products = [c for c in f.children if self.kind.get(c.id) == "product"]
            if f.ptype == "-1" and len(products) >= 2 and len(products) >= 0.6 * len(f.children):
                self.kind[f.id] = "brand" if self._shares_brand(f, products) else "bucket"
            else:
                self.kind[f.id] = "category"

    def _shares_brand(self, f, products):
        own = squash(f.name)
        keys = Counter(squash(brand_key_text(p.name)) for p in products)
        named = sum(1 for p in products
                    if own and (squash(p.name).startswith(own) or own.startswith(squash(brand_key_text(p.name)) or "\0")))
        if own and named >= 0.5 * len(products):
            return True
        key, n = keys.most_common(1)[0]
        if key and n >= 0.6 * len(products):
            return True
        creators = Counter(self.dominant_creator(p) for p in products)
        creators.pop(None, None)
        return bool(creators) and creators.most_common(1)[0][1] >= 0.6 * len(products)


# --------------------------------------------------------------------------
# Triage

class Group(object):
    def __init__(self, key):
        self.key = key
        self.products = []
        self.display = Counter()
        self.match = None          # (destination path, how) when filed automatically
        self.suggestions = []      # [(destination path, score or None, basis)]
        self.split = 0             # existing folders the brand is split across
        self.features = None       # (type vector, word vector), computed once
        self.weak = False          # no filed brand shares its vocabulary
        self.type = None           # a type within a big brand (Bedroom, Skyboxes...)
        self.area = None           # the user's area its Marketplace listings point to
        self.boxed = False         # still-packed products, bound for To Unpack
        self.has_home = False      # the brand already has a brand folder
        self.brand_name = None     # the real brand, when the user corrected a guess

    @property
    def items(self):
        return sum(n for _, n, _ in self.products)

    @property
    def brand(self):
        if self.brand_name:
            return self.brand_name
        return self.display.most_common(1)[0][0] if self.display else self.key

    @property
    def label(self):
        if self.boxed:
            return self.brand + " (boxed)"
        if self.type:
            return self.brand + " / " + self.type
        if self.area:
            return self.brand + " / " + self.area.split(SEP)[-1]
        return self.brand

    def child(self, key, products):
        g = Group(key)
        g.products = products
        g.display = Counter(self.display)
        g.split = self.split
        g.suggestions = list(self.suggestions)
        g.brand_name = self.brand_name
        return g


def text_features(inv, f):
    """Words from the folder and item names, plus one marker token for
    mesh-body fits: the strongest sign that something is worn, since mesh
    clothing and furniture are both plain objects."""
    words = Counter()
    for name in [f.name] + [n for _, _, n in inv.items[f.id]]:
        words.update(w for w in WORD_RE.findall(name.lower()) if w not in STOP_WORDS)
        if icr.BODY_NAMES_RE.search(name):
            words["~bodyfit"] += 3
    return words


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def normalised(counter):
    total = float(sum(counter.values()))
    return Counter({k: v / total for k, v in counter.items()}) if total else Counter()


class Triage(object):
    def __init__(self, inv):
        self.inv = inv
        self.known = {}        # squashed brand name -> brand folder
        self.by_creator = {}   # creator -> brand folder
        self.store_cache = {}
        self.category_areas = {}   # Marketplace category prefix -> area, learnt
        self.groups = []
        self.manual = []       # (path, loose item count) of mixed staging folders
        self._index_destinations()
        self._collect()

    def _organised(self, f):
        """A folder the user has filed things into: not excluded, not staging."""
        return not self.inv.excluded(f) and not self.inv.staging(f)

    def _index_destinations(self):
        """Learn from what is already filed: which brands have folders (and
        where), which creators they belong to, and what each destination
        category tends to hold."""
        inv = self.inv
        self.known = defaultdict(Counter)       # brand key -> {brand folder id: products}
        # Brand folders inside an area for boxed things: only boxed products
        # belong there, so they never match unpacked ones.
        self.boxed_homes = defaultdict(Counter)
        self.own_key = defaultdict(set)         # brand key -> folders named that themselves
        self.folder_creator = {}                # brand folder id -> its products' main creator
        creator_votes = defaultdict(Counter)
        # (destination path, holds brand folders, name, type vector, word vector)
        self.examples = []
        filed = [f for f in inv.folders.values()
                 if inv.kind.get(f.id) in ("brand", "bucket") and self._organised(f)]
        # The user's own category words: names of folders that hold brand or
        # bucket folders (Chairs, Tabletop, Jewellery, Locations...). A folder
        # with one of those names is never taken for a brand.
        category_words = {squash(l) for l in icr.SYSTEM_FOLDER_LABELS.values()}
        held = Counter(f.parent for f in filed)
        for parent_id, n in held.items():
            parent = inv.folders.get(parent_id)
            # Two or more, so "Decoration > Pear Hollow > Art" does not make
            # Pear Hollow a category.
            if n >= 2 and parent is not None and parent is not inv.root:
                category_words.add(squash(parent.name))
        for f in filed:
            products = [c for c in f.children if inv.kind.get(c.id) == "product"]
            own = squash(f.name)
            boxed_area = self.is_boxed_home(f, products)
            branded = len(own) >= 4 and own not in category_words and own not in STOP_WORDS
            keys = {own} if branded else set()
            if branded and not boxed_area:
                self.own_key[own].add(f.id)
            if inv.kind[f.id] == "brand":
                keys |= {squash(brand_key_text(p.name)) for p in products}
                for p in products:
                    c = None if boxed_area else inv.dominant_creator(p)
                    if c:
                        creator_votes[c][f.id] += 1
            for key in keys:
                if len(key) >= 3 and key not in STOP_WORDS and key not in category_words:
                    (self.boxed_homes if boxed_area else self.known)[key][f.id] += len(products)
            types, words = Counter(), Counter()
            for p in products:
                types.update(normalised(inv.types(p)))
                words.update(text_features(inv, p))
            parent = inv.folders.get(f.parent)
            if inv.kind[f.id] == "brand" or branded:
                if parent is None or parent is inv.root:
                    continue
                self.examples.append((inv.path(parent), True, f.name,
                                      normalised(types), normalised(words)))
            else:
                self.examples.append((inv.path(f), False, f.name,
                                      normalised(types), normalised(words)))
        folder_creators = defaultdict(Counter)
        for creator, votes in creator_votes.items():
            for folder_id, n in votes.items():
                folder_creators[folder_id][creator] += n
        self.folder_creator = {fid: c.most_common(1)[0][0] for fid, c in folder_creators.items()}
        for creator, votes in creator_votes.items():
            folder_id, n = votes.most_common(1)[0]
            if n >= 2 or sum(votes.values()) == 1:
                self.by_creator[creator] = inv.folders[folder_id]
        self.brand_parents = {e[0] for e in self.examples if e[1]}

    def in_boxed_area(self, f):
        """The folder, or any folder above it, is named as an area for boxed
        things (Boxed Restraints, Boxed Items, To Unpack)."""
        return any(BOXED_AREA_RE.search(a.name) for a in [f] + list(self.inv.ancestors(f)))

    def is_boxed_home(self, f, products):
        """A brand folder that holds still-packed products. What the products
        are decides (classify_box); the area's name only breaks a tie or
        speaks when nothing is clear, so a folder under "Boxed Restraints" full
        of opened parts is an unpacked home, and a plain-named folder of boxes
        is a boxed one."""
        path = self.inv.path(f)
        if path == TO_UNPACK or path.startswith(TO_UNPACK + SEP):
            return True         # the queue's own brand folders: boxed homes by definition
        # Products with subfolders say little ("has subfolders"); leave them out.
        kinds = Counter(classify_box(self.inv, p)[0] for p in products if not p.children)
        if kinds["BOXED"] != kinds["UNPACKED"]:
            return kinds["BOXED"] > kinds["UNPACKED"]
        return self.in_boxed_area(f)

    @staticmethod
    def boxed_path(path):
        return any(BOXED_AREA_RE.search(part) for part in path.split(SEP))

    def _sources(self):
        inv = self.inv
        for f in inv.folders.values():
            if inv.kind.get(f.id) != "product" or inv.excluded(f):
                continue
            parent = inv.folders.get(f.parent)
            if parent is inv.root or inv.staging(f):
                yield f

    def _match_known(self, f):
        """(brand key, {folder id: products}, how) for a brand that already has
        a folder somewhere, or (None, None, None)."""
        name = squash(f.name)
        best = None
        for key in self.known:
            if name.startswith(key) and (best is None or len(key) > len(best)):
                best = key
        creator = self.inv.dominant_creator(f)
        if best and not self._creator_conflict(best, creator):
            return best, self.known[best], "brand name"
        # The name is of a brand whose only folders are boxed areas: the
        # creator must not smuggle the unpacked product into one of its
        # other folders by the back door. That is the user's decision.
        if not best and any(name.startswith(k) and k not in self.known for k in self.boxed_homes):
            return None, None, None
        if creator in self.by_creator:
            folder = self.by_creator[creator]
            return squash(folder.name), Counter({folder.id: 1}), "creator"
        key = squash(brand_key_text(f.name))
        if key in self.known and not self._creator_conflict(key, creator):
            return key, self.known[key], "brand name"
        return None, None, None

    def _creator_conflict(self, key, creator):
        """A key learnt only from product names inside a brand folder (Curio
        Obscura's "Juniper ..." dresses) does not claim a product by a
        different creator ("{Juniper} Garter" is another brand)."""
        if not creator or self.own_key.get(key):
            return False
        # A key that is part of a folder's own name ("Pale Ember" in "Moon
        # Elixir x LYRA") is that brand, whoever uploaded it: brands use several
        # creator accounts.
        for fid in self.known[key]:
            own = squash(self.inv.folders[fid].name)
            if own.startswith(key) or key.startswith(own):
                return False
        mine = {self.folder_creator.get(fid) for fid in self.known[key]} - {None}
        return bool(mine) and creator not in mine

    def _common_parent(self, folder_ids):
        paths = [self.inv.path(self.inv.folders[i]).split(SEP) for i in folder_ids]
        common = []
        for parts in zip(*paths):
            if len(set(parts)) != 1:
                break
            common.append(parts[0])
        return SEP.join(common)

    def _collect(self):
        inv = self.inv
        auto = {}        # brand key -> Group
        pending = []
        for f in self._sources():
            key, folders, how = self._match_known(f)
            entry = (f, inv.count(f), inv.path(inv.folders.get(f.parent)) or "(root)")
            if key is None:
                pending.append(entry)
                continue
            g = auto.get(key)
            if g is None:
                g = auto[key] = Group(key)
                ranked = [inv.folders[i] for i, _ in folders.most_common()]
                if len(ranked) == 1:
                    g.match = (inv.path(ranked[0]), how)
                else:
                    # The brand already lives in several places (typically
                    # split by type). That is the user's call: offer the
                    # brand-first folder under their common parent, then
                    # the existing places, biggest first.
                    brand = ranked[0].name
                    common = self._common_parent([r.id for r in ranked])
                    options = ([common + SEP + brand] if common else []) + \
                        [inv.path(r) for r in ranked]
                    g.suggestions = [(o, None, "already here" if n else "brand-first")
                                     for n, o in enumerate(options[:3])]
                    g.split = len(ranked)
            g.products.append(entry)
            g.display[inv.folders[folders.most_common(1)[0][0]].name] += 1

        # Unmatched: group by brand name, then join names that share a creator.
        parent = {}

        def find(k):
            while parent.setdefault(k, k) != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        keyed = []
        by_creator = defaultdict(set)
        for entry in pending:
            f = entry[0]
            key = squash(brand_key_text(f.name)) or squash(f.name) or str(f.id)
            keyed.append((key, entry))
            find(key)
            creator = inv.dominant_creator(f)
            if creator:
                by_creator[creator].add(key)
        for keys in by_creator.values():
            keys = sorted(keys)
            for k in keys[1:]:
                parent[find(k)] = find(keys[0])
        # A two-word name with no separator ("TRV mats") keeps both words as
        # its key; if the first word alone is another group's key, it is
        # that brand.
        present = {k for k, _ in keyed}
        for key, entry in keyed:
            words = brand_key_text(entry[0].name).split()
            if len(words) == 2:
                head = squash(words[0])
                if len(head) >= 3 and head != key and head in present and head not in STOP_WORDS:
                    parent[find(key)] = find(head)
        new = {}
        for key, entry in keyed:
            root_key = find(key)
            g = new.get(root_key)
            if g is None:
                g = new[root_key] = Group(root_key)
            g.products.append(entry)
            g.display[brand_key_text(entry[0].name) or entry[0].name] += 1
        for g in new.values():
            canonical = squash(g.display.most_common(1)[0][0])
            g.key = canonical or g.key
            self._suggest(g)
        groups = []
        for g in list(auto.values()) + list(new.values()):
            for c in self._split(g):
                if not c.match and not c.split:
                    self._suggest(c)
                groups.append(c)
        self.groups = sorted([g for g in groups if g.match], key=lambda g: -len(g.products)) + \
            sorted([g for g in groups if not g.match], key=lambda g: (-len(g.products), -g.items))

        for f in inv.folders.values():
            if inv.kind.get(f.id) == "category" and inv.staging(f) and not inv.excluded(f) \
                    and inv.items[f.id]:
                self.manual.append((inv.path(f), len(inv.items[f.id])))
        self.manual.sort(key=lambda m: -m[1])

    def _split(self, g):
        """Boxed products go to the To Unpack queue under their brand; a big
        brand's unpacked products split into one decision per type."""
        boxed = [e for e in g.products if is_boxed(self.inv, e[0])]
        rest = [e for e in g.products if not is_boxed(self.inv, e[0])]
        textures = [e for e in rest if is_textures(self.inv, e[0])]
        unpacked = [e for e in rest if not is_textures(self.inv, e[0])]
        out = []
        if boxed:
            b = g.child("box:" + g.key, boxed)
            b.boxed = True
            b.match = (TO_UNPACK + SEP + g.brand, "boxed")
            b.has_home = bool(g.match)
            out.append(b)
        if textures:
            t = g.child(g.key + "/Textures", textures)
            t.type = "Textures"
            t.match = None
            out.append(t)
        if not unpacked:
            return out
        if g.match or len(unpacked) < TYPE_SPLIT_MIN:
            rest = g.child(g.key, unpacked)
            rest.match = g.match
            out.append(rest)
            return out
        by_type = defaultdict(list)
        for e in unpacked:
            by_type[product_type(self.inv, e[0])].append(e)
        real = [t for t, es in by_type.items() if t and len(es) >= 2]
        if len(real) < 2:
            out.append(g.child(g.key, unpacked))
            return out
        others = []
        for t, es in by_type.items():
            if t not in real:
                others.extend(es)       # one decision for the odd ones out
                continue
            c = g.child(g.key + "/" + t, es)
            c.type = t
            if g.split:
                c.suggestions = [(d + SEP + t, s, b) for d, s, b in g.suggestions]
            out.append(c)
        if others:
            out.append(g.child(g.key, others))
        return out

    def _features(self, g):
        if g.features is None:
            types, words = Counter(), Counter()
            for f, _, _ in g.products:
                types.update(normalised(self.inv.types(f)))
                words.update(text_features(self.inv, f))
            g.features = (normalised(types), normalised(words))
        return g.features

    def learn(self, g, dest):
        """Use a decision as a filed example, so brands like it are
        suggested the same home (this is how a new category, such as
        Landscaping, starts attracting the right brands)."""
        types, words = self._features(g)
        name = g.brand
        parts = dest.split(SEP)
        if g.type and len(parts) > 1 and squash(parts[-1]) == squash(g.type):
            parts = parts[:-1]
            dest = SEP.join(parts)
        if len(parts) > 1 and squash(parts[-1]) == squash(name):
            self.examples.append((SEP.join(parts[:-1]), True, name, types, words))
            self.brand_parents.add(SEP.join(parts[:-1]))
        else:
            self.examples.append((dest, False, name, types, words))

    def use_store_cache(self, cache):
        """Learn which of the user's areas each Marketplace category maps to
        from brands already filed whose stores are cached, then refresh the
        suggestions."""
        self.store_cache = cache or {}
        votes = defaultdict(Counter)
        for key, folders in self.known.items():
            entry = self.store_cache.get(key)
            if not entry or not entry["listings"]:
                continue
            folder_id, _ = folders.most_common(1)[0]
            folder = self.inv.folders[folder_id]
            parent = self.inv.folders.get(folder.parent)
            if parent is None or parent is self.inv.root or self.inv.kind.get(folder_id) != "brand":
                continue
            area = self.inv.path(parent)
            # One vote per brand and category prefix (three listings or
            # more), so a store with 1,800 listings does not outvote the
            # rest of the inventory.
            mine = Counter()
            for _, category in entry["listings"]:
                for n in range(1, len(category) + 1):
                    mine[SEP.join(category[:n])] += 1
            # ...and only for what the brand mainly sells: a clothing
            # brand's few decor gifts must not teach "Home and Garden".
            total = float(len(entry["listings"]))
            for prefix, n in mine.items():
                if n >= 3 and n / total >= LEARN_MIN_SHARE:
                    votes[prefix][area] += 1
        self.category_areas = {}
        for prefix, areas in votes.items():
            area, n = areas.most_common(1)[0]
            total = sum(areas.values())
            seed = area_for_category(prefix.split(SEP), None, self.exists)
            if seed is None:
                if n * 2 > total:
                    self.category_areas[prefix] = area     # a category the seed map lacks
            elif n >= 2 and n * 2 > total and area.split(SEP)[0] == seed.split(SEP)[0]:
                # Two brands agreeing refine the seed (Objects > Building ->
                # Objects > Building > Tabletop); they never contradict it.
                self.category_areas[prefix] = area
        groups = []
        for g in self.groups:
            groups.extend(self._split_by_marketplace(g) or [g])
        self.groups = groups
        for g in self.groups:
            if not g.match and not g.split:
                self._suggest(g)

    def _split_by_marketplace(self, g):
        """A brand whose matched listings fall into two or more of the
        user's areas, each with at least two products, becomes one decision
        per area (TRV sells boots and restraints: Clothes and Kink)."""
        if g.match or g.split or g.boxed or g.type or g.area:
            return None
        entry = self.store_cache.get(brand_key(g))
        if not entry or not entry["listings"]:
            return None
        by_area = defaultdict(list)
        for e in g.products:
            m = match_listing(e[0].name, entry["listings"])
            area = None
            if m and m[1]:
                area = area_for_category(list(m[1]), self.category_areas, self.exists)
            by_area[area].append(e)
        real = [a for a, es in by_area.items() if a and len(es) >= 2]
        if len(real) < 2:
            return None
        out, others = [], []
        for area, es in by_area.items():
            if area not in real:
                others.extend(es)       # unmatched or lone ones: one decision together
                continue
            c = g.child(g.key + "/" + squash(area.split(SEP)[-1]), es)
            c.area = area
            out.append(c)
        if others:
            out.append(g.child(g.key, others))
        return out

    def _marketplace_suggestion(self, g):
        """(destination area, basis) from the brand's Marketplace listings, or None."""
        cats, how = marketplace_categories(self.store_cache, g)
        if not cats:
            return None
        areas = Counter()
        for category, n in cats.items():
            area = area_for_category(list(category), self.category_areas, self.exists)
            if area:
                areas[area] += n
        if not areas:
            return None
        area, n = areas.most_common(1)[0]
        top = Counter(SEP.join(c[:2]) for c in cats.elements()).most_common(1)[0][0]
        share = int(round(100.0 * n / sum(areas.values())))
        if how == "listing":
            basis = "Marketplace listings (%d of %d matched): %s %d%%" % (
                sum(cats.values()), len(g.products), top, share)
        else:
            basis = "Marketplace store mix, no product matched: %s %d%%" % (top, share)
        return area, basis, how

    def apply_brand_names(self, names):
        """The user's corrections of guessed brands ({"pierced": "Verne
        Industries"}): the group is shown, suggested and boxed under the real
        name."""
        for g in self.groups:
            name = names.get(brand_key(g))
            if not name:
                continue
            g.brand_name = name
            if g.boxed:
                g.match = (TO_UNPACK + SEP + name, "boxed")

    def apply_decisions(self, decisions):
        """Learn from decisions saved by earlier runs, then refresh the
        suggestions for everything still undecided."""
        for g in self.groups:
            if not g.match and g.key in decisions:
                self.learn(g, decisions[g.key]["dest"])
        for g in self.groups:
            if not g.match and not g.split and g.key not in decisions:
                self._suggest(g)

    def _suggest(self, g):
        types, words = self._features(g)
        name = g.brand
        if g.type == "Textures":
            # Texture-only products go with the textures, under the brand.
            areas = [a for a in TEXTURE_AREAS if self.exists(a)] or TEXTURE_AREAS[:1]
            g.weak = False
            g.suggestions = [(a + SEP + name, None, "textures and materials only") for a in areas]
            return
        # Compare with each filed brand, not with whole categories: a big,
        # broad category (Clothes) is a middling match for everything, while
        # the few most similar brands say specifically where this belongs.
        # Types separate worn from rezzed only partly (mesh clothing and
        # furniture are both objects), so words carry equal weight.
        scored = [(0.5 * cosine(types, t) + 0.5 * cosine(words, w), cosine(words, w), path, holds, ex)
                  for path, holds, ex, t, w in self.examples
                  if not self.boxed_path(path + SEP + ex if holds else path)]
        scored.sort(key=lambda s: -s[0])
        sims = scored[:SIMILAR_BRANDS]
        # With no shared vocabulary the match rests on item types alone,
        # which cannot tell landscaping from clothing (both are objects).
        g.weak = max((s[1] for s in scored), default=0.0) < WEAK_WORD_MATCH
        votes = defaultdict(float)
        like = defaultdict(list)
        holds_of = {}
        for sim, _, path, holds, ex in sims:
            # Brand-first: a brand parent nested inside another (Building >
            # Tables) wins only on a clearly better match than its ancestor.
            nested = any(path.startswith(other + SEP) for other in self.brand_parents)
            # Best match per destination, not a sum: a category with many
            # brands (Clothes) must not win on numbers alone.
            votes[path] = max(votes[path], sim * (0.85 if nested else 1.0))
            like[path].append(ex)
            holds_of[path] = holds
        ranked = sorted(votes.items(), key=lambda kv: -kv[1])[:3]
        suffix = SEP + g.type if g.type else ""
        g.suggestions = [((path + SEP + name if holds_of[path] else path) + suffix, None,
                          ("weak match, nothing similar filed yet" if g.weak else
                           "like " + ", ".join(like[path][:3])))
                         for path, _ in ranked]
        # The brand's own Marketplace listings say what it sells; that beats
        # similarity to other brands, so it goes first.
        # The store's overall mix is weaker evidence: adult listings are
        # hidden from a visitor, so it leads only when nothing similar is
        # filed; otherwise it comes second.
        mp = self._marketplace_suggestion(g)
        if mp:
            area, basis, how = mp
            dest = area + SEP + name + suffix
            rest = [s for s in g.suggestions if s[0] != dest]
            if how == "listing" or g.weak:
                g.weak = False
                g.suggestions = [(dest, None, basis)] + rest[:2]
            else:
                g.suggestions = rest[:1] + [(dest, None, basis)] + rest[1:2]

    def exists(self, path):
        return any(self.inv.path(f) == path for f in self.inv.folders.values())


# --------------------------------------------------------------------------
# Decisions, display and the plan

def load_map(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("decisions", {})
        data.setdefault("skipped", [])
        data.setdefault("brands", {})
        return data
    return {"version": 1, "decisions": {}, "skipped": [], "brands": {}}


def save_map(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def describe(tri, g, index, total, out):
    inv = tri.inv
    types = Counter()
    for f, _, _ in g.products:
        types.update(inv.types(f))
    mix = ", ".join("%s %d%%" % (icr.ASSET_TYPE_LABELS.get(t, t), round(100.0 * n / sum(types.values())))
                    for t, n in types.most_common(3)) if types else "no items"
    out.write("\n[%d/%d] %s  -  %d product folder%s, %d items (%s)\n" % (
        index, total, g.label, len(g.products),
        "" if len(g.products) == 1 else "s", g.items, mix))
    for f, n, where in g.products[:4]:
        out.write("    %s  [%d]  in %s\n" % (f.name[:70], n, where))
    if len(g.products) > 4:
        out.write("    ... and %d more (l to list)\n" % (len(g.products) - 4))
    mp = marketplace_summary(tri.store_cache, g) if getattr(tri, "store_cache", None) else None
    if mp:
        out.write("  Marketplace store: %s\n" % mp)


def destination_of(g, decisions):
    if g.match:
        return g.match[0], "existing brand folder (%s)" % g.match[1]
    d = decisions.get(g.key)
    if d:
        return d["dest"], "your decision"
    return None, None


# --------------------------------------------------------------------------
# Naming: follow the brand folder's convention

# Words that name a kind of product or one of its parts, not the product.
TYPE_WORDS = set("hair outfit set fatpack pack hud huds box".split())
LEAD_SEP_RE = re.compile(r"^[\s\-:/|.\~_,]+")
SEP_CHARS = set("-:/|.\\~_,")
TAG_CHARS = "-~+*=•♥★.:|"
LEAD_TAG_RE = re.compile(
    r"^\s*(?:[\[\{<\(][^\]\}\)>]{1,40}[\]\}\)>]"
    r"|([" + TAG_CHARS + r"]{1,3})[^" + TAG_CHARS + r"\s][^" + TAG_CHARS + r"]{0,38}?\1)")
TRAIL_VERSION_RE = re.compile(
    r"[\s\-_]*(?:\(\s*v?\d+(?:\.\d+)*\s*\)|\bv\d+(?:\.\d+)*|\b\d+(?:\.\d+)+)\s*$", re.IGNORECASE)


def _is_type_word(word):
    w = re.sub(r"[^\w]", "", word).lower()
    if not w:
        return False
    if w in TYPE_WORDS or PART_NAME_RE.match(w):
        return True
    return any(rx.fullmatch(w) for _, rx in PRODUCT_TYPE_RES)


def _strip_brand_prefix(name, keys):
    """Drop a leading brand tag ("[BW] ", "-MENDED- ") or the brand followed
    by a separator ("Marigold - ", "Cedar. ")."""
    m = LEAD_TAG_RE.match(name)
    if m:
        return name[m.end():]
    for m in re.finditer(r"\w", name):
        i = m.end()
        if i < len(name) and re.match(r"\w", name[i]):
            continue
        if squash(name[:i]) in keys:
            rest = name[i:]
            seps = LEAD_SEP_RE.match(rest)
            if seps and set(seps.group(0)) & SEP_CHARS:
                return rest[seps.end():]
            break
    return name


def propose_name(name, keys, sibling_names):
    """The product name with brand, type word and version removed, as far as
    the brand folder's own products do without them; "" when the name carries
    no brand or nothing sensible is left."""
    ends = {re.sub(r"[^\w]", "", n.split()[-1]).lower() for n in sibling_names if n.split()}
    versioned = any(TRAIL_VERSION_RE.search(n) for n in sibling_names)
    cur = name.strip()
    for _ in range(2):      # a tag and then the brand: "[BW] Bramblewick - Bikini"
        cur = LEAD_SEP_RE.sub("", _strip_brand_prefix(cur, keys)).strip()
    if cur == name.strip():
        return ""           # no brand to remove: the name is not in the brand's style to begin with
    if _is_type_word(cur):      # "Marigold - Hair": a part, not a product name
        return ""
    while True:
        before = cur
        words = cur.split()
        if len(words) > 1 and _is_type_word(words[-1]) \
                and re.sub(r"[^\w]", "", words[-1]).lower() not in ends:
            cur = " ".join(words[:-1])
        if not versioned:
            cur = TRAIL_VERSION_RE.sub("", cur)
        cur = re.sub(r"[\s\-:/|.\~_,]+$", "", cur)
        if cur == before:
            return cur


def naming_siblings(inv, dest_folders, keys):
    """The existing products of a brand folder, when they name themselves by
    product only (at least 2 products, two thirds without the brand)."""
    for f in dest_folders:
        if inv.kind.get(f.id) not in ("brand", "bucket"):
            continue
        products = [c for c in f.children if inv.kind.get(c.id) == "product"]
        if len(products) < 2:
            continue
        bare = sum(1 for p in products if not any(k in squash(p.name) for k in keys))
        if 3 * bare >= 2 * len(products):
            return f, [c.name for c in f.children]
    return None, []


def proposed_renames(tri, g, dest, folders_by_path):
    """{folder id: new name} for the products of g that should be renamed to
    match the existing brand folder dest."""
    if g.boxed or dest == TO_UNPACK or dest.startswith(TO_UNPACK + SEP):
        return {}
    inv = tri.inv
    own = squash(dest.split(SEP)[-1])
    keys = {k for k in (squash(g.brand), squash(g.key)) if len(k) >= 2}
    if own not in keys:
        return {}       # the folder is a category of several brands, not this brand's
    keys.add(own)
    folder, names = naming_siblings(inv, folders_by_path.get(dest, []), keys)
    if folder is None:
        return {}
    taken = {n.lower() for n in names}
    out = {}
    for f, _, _ in g.products:
        if BOX_NAME_RE.search(f.name):
            continue
        new = propose_name(f.name, keys, names)
        if len(new) >= 2 and new != f.name and new.lower() not in taken:
            out[f.id] = new
    # Two incoming folders must not end up with the same name.
    counts = Counter(n.lower() for n in out.values())
    return {i: n for i, n in out.items() if counts[n.lower()] == 1}


def interactive(tri, map_path, data, ask=input, out=sys.stdout):
    decisions = data["decisions"]
    todo = [g for g in tri.groups if not g.match and g.key not in decisions
            and g.key not in data["skipped"]]
    auto = [g for g in tri.groups if g.match]
    out.write("%d product folders in %d brands match an existing brand folder and need no "
              "decision.\n%d brands to decide (%d already decided, %d skipped). Decisions "
              "save as you go to %s\n" % (
                  sum(len(g.products) for g in auto), len(auto), len(todo),
                  len(decisions), len(data["skipped"]), map_path))
    for i, g in enumerate(todo, 1):
        if not g.split:
            tri._suggest(g)    # now, so every decision made so far counts
        describe(tri, g, i, len(todo), out)
        if g.split:
            out.write("  This brand already has folders in %d places; pick one home for it.\n"
                      % g.split)
        elif g.weak:
            out.write("  Nothing similar is filed yet, so these are guesses. Type a path to "
                      "start a new area\n  (e.g. Objects > Landscaping > %s); later brands like "
                      "it will then be suggested there.\n" % g.brand)
        for n, (dest, score, basis) in enumerate(g.suggestions, 1):
            out.write("  %d) %s%s  (%s)\n" % (
                n, dest, "" if tri.exists(dest) else "  [new folder]",
                basis if score is None else "similarity %.2f" % score))
        while True:
            answer = ask("  Enter = 1, 2/3 = other suggestion, or type a path "
                         "(Category > Sub > Brand); s = skip, l = list, q = save and quit: ").strip()
            if answer.lower() == "q":
                save_map(map_path, data)
                out.write("Saved. Run again to continue where you left off.\n")
                return
            if answer.lower() == "l":
                for f, n, where in g.products:
                    out.write("    %s  [%d]  in %s\n" % (f.name[:70], n, where))
                continue
            if answer.lower() == "s":
                data["skipped"].append(g.key)
                break
            if answer in ("", "1", "2", "3"):
                pick = int(answer or "1") - 1
                if pick >= len(g.suggestions):
                    out.write("  no such suggestion\n")
                    continue
                dest = g.suggestions[pick][0]
            else:
                dest = SEP.join(p.strip() for p in re.split(r"\s*>\s*", answer) if p.strip())
                if not dest:
                    continue
            decisions[g.key] = {"dest": dest, "brand": g.label,
                                "at": datetime.datetime.now().isoformat(timespec="seconds")}
            out.write("  -> %s%s\n" % (dest, "" if tri.exists(dest) else "  [new folder]"))
            tri.learn(g, dest)
            break
        save_map(map_path, data)
    out.write("\nAll brands decided or skipped. Write the plan with --plan plan.csv\n")


def list_groups(tri, data, limit, out=sys.stdout):
    decisions = data["decisions"]
    auto = [g for g in tri.groups if g.match]
    rest = [g for g in tri.groups if not g.match]
    out.write("Automatic: %d product folders (%d items) in %d brands with an existing folder\n" % (
        sum(len(g.products) for g in auto), sum(g.items for g in auto), len(auto)))
    for g in auto[:limit]:
        out.write("  %-34s %3d folders  -> %s  (by %s)\n" % (
            g.label[:34], len(g.products), g.match[0], g.match[1]))
    out.write("\nNeeds a decision: %d product folders (%d items) in %d brands\n" % (
        sum(len(g.products) for g in rest), sum(g.items for g in rest), len(rest)))
    covered = 0
    for n, g in enumerate(rest, 1):
        covered += len(g.products)
        if n <= limit:
            done = decisions.get(g.key)
            best = g.suggestions[0] if g.suggestions else ("?", 0, "")
            out.write("  %-34s %3d folders  %s %s\n" % (
                g.label[:34], len(g.products),
                "decided ->" if done else "suggest ->", done["dest"] if done else best[0]))
        if n in (10, 25, 50, 100):
            out.write("    (top %d brands cover %d folders)\n" % (n, covered))
    if tri.manual:
        out.write("\nMixed folders to sort by hand (not product folders): %d\n" % len(tri.manual))
        for path, n in tri.manual[:limit]:
            out.write("  %4d loose items  %s\n" % (n, path))


STORE_FIELDS = ["brand", "store_url", "search_link", "folders", "needs_help",
                "example_product", "key"]
SEARCH_URL = "https://marketplace.secondlife.com/products/search?search%5Bkeywords%5D="


def brand_key(g):
    """One key per brand, across its type and boxed groups."""
    return g.key[4:] if g.key.startswith("box:") else g.key.split("/", 1)[0]


def write_stores(tri, data, path, out=sys.stdout):
    """Write (or refresh) a CSV of brands without a home, for you to fill in
    Marketplace store URLs. URLs already typed into the file are kept, as
    are rows for brands that have since been filed."""
    kept = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if row.get("key"):
                    kept[row["key"]] = row
    brands = {}
    for g in tri.groups:
        if (g.match and not g.boxed) or g.has_home:
            continue                      # already has a brand folder
        key = brand_key(g)
        b = brands.setdefault(key, {"brand": g.brand, "folders": 0, "weak": False,
                                    "example": g.products[0][0].name, "decided": True})
        b["folders"] += len(g.products)
        b["weak"] = b["weak"] or g.weak
        if not g.boxed and g.key not in data["decisions"]:
            b["decided"] = False
    rows = []
    for key, b in brands.items():
        old = kept.pop(key, {})
        rows.append({"brand": b["brand"], "folders": b["folders"],
                     "search_link": SEARCH_URL + quote_plus(b["brand"]),
                     "needs_help": "yes" if b["weak"] and not b["decided"] else "",
                     "example_product": b["example"], "store_url": old.get("store_url", ""),
                     "key": key})
    rows.sort(key=lambda r: (r["needs_help"] != "yes", -r["folders"], r["brand"].lower()))
    rows.extend(kept.values())             # brands no longer unfiled keep their URL
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=STORE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    filled = sum(1 for r in rows if r.get("store_url"))
    out.write("Store list written to %s: %d brands (%d marked needs_help, the ones with "
              "nothing similar filed), %d with a store URL so far.\n"
              "Open it in Excel: click a search_link to find the brand's store, then paste "
              "the store's page address (marketplace.secondlife.com/stores/...) into store_url "
              "on the same row.\n"
              "Leave rows you don't know blank, and keep the file as CSV when saving. "
              "Run this again any time; your URLs are kept.\n" % (
                  path, len(rows), sum(1 for r in rows if r.get("needs_help") == "yes"), filled))
    return rows


# --------------------------------------------------------------------------
# Marketplace store pages
#
# Only store pages listed in the store CSV are fetched (its robots.txt
# forbids /search for robots), one at a time, and every page is cached so it
# is fetched once. Pacing (the user's choice): a random 2.5-5.5 s gap between
# requests and a 15-30 s rest every 20. Parallel readers at about two
# requests a second get the connection blocked by Akamai. A block page
# carries no listings and is not a Marketplace page at all, so that (or an
# HTTP 403/429) stops the run at once rather than caching the store as
# finished. A real store page with no listings is different: an adult-only
# store shows nothing to a visitor who is not signed in. That is recorded as
# a store with no public listings and the run moves on; fetch it again with
# --cookie-file after deleting its entry from the cache. Each listing on a store page embeds its name, brand and category:
#   var product_dl_data_N = { 'name': '...', 'brand': '...',
#                             'category': '[&quot;Home and Garden&quot;, &quot;Decor&quot;]' };

STORE_URL_RE = re.compile(r"^https?://marketplace\.secondlife\.com/stores/(\d+)")
LISTING_RE = re.compile(r"var product_dl_data_\d+\s*=\s*\{(.*?)\};", re.S)
LISTING_FIELD_RE = re.compile(r"'(name|brand|category)'\s*:\s*'((?:[^'\\]|\\.)*)'")
# Ask for 96 listings a page (the most the site offers). If a page comes
# back with fewer than 48, the smallest page size, it was the last one;
# otherwise the next page is fetched, which ends the store if it is empty.
STORE_PER_PAGE = 96
STORE_PAGE_SIZE = 48
STORE_MAX_PAGES = 20
STORE_PAGE_MARK = "Second Life Marketplace"    # in every real Marketplace page's title
CRAWL_GAP = (2.5, 5.5)       # seconds between requests
CRAWL_REST = (15.0, 30.0)    # longer pause...
CRAWL_REST_EVERY = 20        # ...after this many requests


class MarketplaceBlocked(Exception):
    """The Marketplace refused a page: stop without touching the cache."""


def parse_store_page(page):
    """[(listing name, [category, subcategory...])] from one store page."""
    import html
    out = []
    for block in LISTING_RE.findall(page):
        fields = dict(LISTING_FIELD_RE.findall(block))
        try:
            category = json.loads(html.unescape(fields.get("category", "[]")))
        except ValueError:
            category = []
        name = html.unescape(fields.get("name", "")).replace("\\'", "'")
        if name:
            out.append((name, [str(c) for c in category]))
    return out


def load_store_cache(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def fetch_stores(stores_csv, cache_path, out=sys.stdout, fetch=None, sleep=None,
                 cookie=None, delay=None, rand=None, private_only=False):
    """Fetch every store page listed in the store CSV that is not cached yet.
    Safe to stop and rerun: finished stores are never fetched again.

    `delay` fixes the gap between requests (the default is a random one in
    CRAWL_GAP, with a CRAWL_REST pause every CRAWL_REST_EVERY requests). A
    blocked or empty page raises MarketplaceBlocked; nothing from it is cached.

    `private_only` (with a cookie) refetches only the stores an earlier run
    cached as having no public listings, and touches nothing else: the split
    between a public crawl elsewhere and a signed-in run at home.

    Adult listings only appear to a signed-in account. `cookie` is the Cookie
    header from the user's own signed-in browser session (read from a local
    file); no password is ever handled here."""
    import time
    import urllib.request
    headers = {"User-Agent": "inventory triage (personal use)"}
    if cookie:
        headers["Cookie"] = cookie
    import urllib.error

    def default_fetch(url):
        try:
            return urllib.request.urlopen(urllib.request.Request(
                url, headers=headers), timeout=60).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise MarketplaceBlocked("HTTP %d from the Marketplace" % e.code)
            raise
    fetch = fetch or default_fetch
    sleep = sleep or time.sleep
    rand = rand or random.uniform
    requests = 0
    cache = load_store_cache(cache_path)
    with open(stores_csv, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("store_url") or "").strip()]
    first = True
    for row in rows:
        m = STORE_URL_RE.match(row["store_url"].strip())
        if not m:
            out.write("skipping %s: not a marketplace.secondlife.com/stores/ link\n" % row["brand"])
            continue
        store = m.group(1)
        if private_only:
            entry = cache.get(row["key"])
            if not entry or entry.get("public", True):
                continue
            entry.update({"listings": [], "pages": 0, "done": False, "public": True})
        entry = cache.setdefault(row["key"], {"store": store, "listings": [], "pages": 0,
                                              "done": False})
        while not entry["done"]:
            if not first:
                if delay is not None:
                    sleep(delay)
                elif requests % CRAWL_REST_EVERY == 0:
                    sleep(rand(*CRAWL_REST))
                else:
                    sleep(rand(*CRAWL_GAP))
            first = False
            page = entry["pages"] + 1
            url = ("https://marketplace.secondlife.com/stores/%s?search%%5Bper_page%%5D=%d&page=%d"
                   % (store, STORE_PER_PAGE, page))
            out.write("fetching %s page %d...\n" % (row["brand"], page))
            requests += 1
            body = fetch(url)
            listings = parse_store_page(body)
            if not listings and ("Access Denied" in body or STORE_PAGE_MARK not in body):
                raise MarketplaceBlocked(
                    "no Marketplace page for %s page %d: the Marketplace is probably blocking "
                    "this connection. Stopped; rerun later and it will resume here." % (
                        row["brand"], page))
            if not listings and page == 1:
                if cookie:
                    # Empty even signed in: an empty or closed store, not
                    # one to keep retrying with --private-only.
                    entry["empty"] = True
                    out.write("  %s: no listings even signed in (empty or closed store)\n"
                              % row["brand"])
                else:
                    entry["public"] = False
                    out.write("  %s: no listings visible without signing in (an adult "
                              "store?); --private-only --cookie-file refetches it\n"
                              % row["brand"])
            entry["listings"].extend(listings)
            entry["pages"] = page
            entry["done"] = len(listings) < STORE_PAGE_SIZE or page >= STORE_MAX_PAGES
            with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(cache_path + ".tmp", cache_path)
    out.write("Store cache %s: %d stores.\n" % (cache_path, len(cache)))
    return cache


# Marketplace categories -> the user's areas. The longest matching prefix
# wins; where several area names are listed, the first one that exists in
# the inventory is used (different avatars name their areas differently),
# else the first. The brand folder goes under the area. Areas learnt from
# brands already filed whose stores are cached take precedence over this.
CATEGORY_AREAS = {
    # Category names are the Marketplace's own, as seen in 7,000 cached
    # listings on 2 October 2026 (Apparel, Avatar Components, Avatar
    # Accessories, Celebrations, Home and Garden...).
    "Apparel": ["Clothing > Clothes", "Clothing"],
    "Apparel > Women's > Women's Footwear": ["Clothing > Clothes", "Clothing > Shoes"],
    "Apparel > Men's > Men's Footwear": ["Clothing > Clothes", "Clothing > Shoes"],
    "Apparel > Unisex > Unisex Footwear": ["Clothing > Clothes", "Clothing > Shoes"],
    "Apparel > Women's > Women's Costumes > Women's Latex and BDSM Costumes":
        ["Clothing > Latex & Fetish", "Clothing > Clothes", "Clothing"],
    "Apparel > Women's > Women's Intimates > Stockings":
        ["Clothing > Clothes", "Clothing > Hosiery", "Clothing"],
    "Celebrations": ["Clothing > Clothes", "Clothing"],       # seasonal clothing
    "Avatar Accessories": ["Clothing > Accessories", "Clothing"],
    "Avatar Accessories > BDSM Accessories":
        ["Clothing > Accessories > Kink", "Clothing > Kink", "Objects > BDSM & Restraints"],
    "Avatar Accessories > Jewelry and Watches":
        ["Clothing > Accessories > Jewellery", "Clothing > Accessories", "Clothing"],
    "Avatar Accessories > Glasses":
        ["Clothing > Accessories > Glasses", "Clothing > Accessories", "Clothing"],
    "Avatar Accessories > Hair": ["Body Parts > Hair", "Body Parts"],
    "Avatar Accessories > Hair > Hair Accessories": ["Clothing > Accessories", "Clothing"],
    "Avatar Accessories > Genitalia": ["Body Parts > Avatar", "Body Parts"],
    "Avatar Accessories > HUD Attachments": ["Objects > Utilities", "Objects"],
    "Avatar Components": ["Body Parts"],
    "Avatar Components > Avatar Skins": ["Body Parts > Skin", "Body Parts > Skins", "Body Parts"],
    "Avatar Components > Avatar Shapes": ["Body Parts > Shapes", "Body Parts"],
    "Avatar Components > Eyes": ["Body Parts > Eyes", "Body Parts"],
    "Avatar Components > Hair": ["Body Parts > Hair", "Body Parts"],
    "Avatar Components > Tattoos":
        ["Body Parts > Cosmetics", "Body Parts > BoM Detailing", "Body Parts > Tattoos", "Body Parts"],
    "Avatar Components > Cosmetic Enhancements":
        ["Body Parts > Cosmetics", "Body Parts > BoM Detailing", "Body Parts"],
    "Avatar Components > Mesh Bodies": ["Body Parts > Bodies", "Body Parts"],
    "Avatar Components > Mesh Heads": ["Body Parts > Heads", "Body Parts"],
    "Home and Garden": ["Objects > Building", "Objects"],
    "Home and Garden > Landscaping": ["Objects > Landscaping", "Objects > Building", "Objects"],
    "Home and Garden > Garden": ["Objects > Landscaping", "Objects > Building", "Objects"],
    "Home and Garden > Plants": ["Objects > Landscaping", "Objects > Building", "Objects"],
    "Buildings and Other Structures": ["Objects > Building > Housing", "Objects > Building", "Objects"],
    "Buildings and Other Structures > Skyboxes":
        ["Objects > Building > Locations", "Objects > Building", "Objects"],
    "Landscaping": ["Objects > Landscaping", "Objects > Building", "Objects"],
    "Building and Object Components": ["Materials", "Objects > Building", "Objects"],
    "Animations": ["Animations"],
    "Gestures": ["Gestures"],
    "Scripts": ["Scripts"],
    "Audio and Video": ["Objects"],
    "Art": ["Objects > Building > Decoration", "Objects > Building", "Objects"],
    "Gadgets": ["Objects > Utilities", "Objects"],
    "Animated Objects": ["Objects"],
    "Animals": ["Objects"],
    "Business": ["Objects"],
    "Vehicles": ["Objects"],
    "Weapons": ["Objects"],
    "Recreation and Entertainment": ["Objects"],
    "Everything Else": ["Objects"],
}
LISTING_MATCH_MIN = 0.6    # share of a product folder's words a listing must contain
LEARN_MIN_SHARE = 0.2      # share of a filed brand's listings a category needs to teach its area
LISTING_MATCH_WORDS = 2    # ...and at least this many of them
LISTING_NOISE = set("box boxed demo unpack unpacker unpacked wear add me hud fatpack "
                    "fat pack full pbr bom v1 v2 v3 new".split())


def area_for_category(category, learnt=None, exists=None):
    """The user's area for a Marketplace category path (a list), by the
    longest prefix found in the learnt map, then the seed map."""
    for n in range(len(category), 0, -1):
        prefix = SEP.join(category[:n])
        if learnt and prefix in learnt:
            return learnt[prefix]
        if prefix in CATEGORY_AREAS:
            options = CATEGORY_AREAS[prefix]
            if exists:
                for o in options:
                    if exists(o):
                        return o
            return options[0]
    return None


def listing_words(name):
    """Words that identify a product: not stop words, packaging noise or
    mesh-body names (a listing title lists every body it fits)."""
    return {w for w in WORD_RE.findall(name.lower())
            if w not in STOP_WORDS and w not in LISTING_NOISE
            and not icr.BODY_NAMES_RE.search(w)}


def match_listing(name, listings):
    """(listing name, category) of the listing most like a product folder
    name, or None when nothing overlaps enough."""
    words = listing_words(name)
    if not words:
        return None
    best, best_score = None, (0.0, 0.0)
    for lname, category in listings:
        lw = listing_words(lname)
        if not lw:
            continue
        shared = len(words & lw)
        # How much of the product name the listing covers, then how close
        # the two are overall (a listing title lists sizes and bodies too).
        score = (shared / float(len(words)), shared / float(len(words | lw)))
        if score > best_score:
            best, best_score = (lname, category), score
    if best is None or best_score[0] < LISTING_MATCH_MIN:
        return None
    if len(listing_words(best[0]) & words) < min(LISTING_MATCH_WORDS, len(words)):
        return None
    return best


def marketplace_categories(cache, g):
    """(category Counter, how) for a group: the categories of the listings
    matching its product folders by name, else the store's overall mix."""
    entry = cache.get(brand_key(g)) if cache else None
    if not entry or not entry["listings"]:
        return None, None
    matched = Counter()
    for f, _, _ in g.products:
        m = match_listing(f.name, entry["listings"])
        if m and m[1]:
            matched[tuple(m[1])] += 1
    if matched:
        return matched, "listing"
    return Counter(tuple(c) for _, c in entry["listings"] if c), "store"


def marketplace_summary(cache, g):
    """The store's main categories, and how many of the group's product
    folders matched a listing by name."""
    entry = cache.get(brand_key(g))
    if not entry or not entry["listings"]:
        return None
    tops = Counter(" > ".join(c[:2]) for _, c in entry["listings"] if c)
    total = float(sum(tops.values())) or 1.0
    main = ", ".join("%s %d%%" % (c, round(100 * n / total)) for c, n in tops.most_common(3))
    cats, how = marketplace_categories(cache, g)
    if how == "listing":
        hit = sum(cats.values())
        main += "; %d of %d product%s matched a listing" % (
            hit, len(g.products), "" if len(g.products) == 1 else "s")
    return main


PLAN_FIELDS = ["product_folder", "items", "from", "to", "because", "creates_folder",
               "folder_id", "from_id", "rename_to", "allow_protected"]


def write_plan(tri, data, path, out=sys.stdout):
    rows, undecided = [], 0
    new_folders = set()
    folders_by_path = defaultdict(list)
    for fo in tri.inv.folders.values():
        folders_by_path[tri.inv.path(fo)].append(fo)
    for g in tri.groups:
        dest, how = destination_of(g, data["decisions"])
        if dest is None:
            undecided += len(g.products)
            continue
        if not tri.exists(dest):
            new_folders.add(dest)
        decision = data["decisions"].get(g.key) if how == "your decision" else None
        # A new name only makes sense for a single product folder.
        rename = (decision or {}).get("rename", "") if len(g.products) == 1 else ""
        if decision and decision.get("rename") and len(g.products) > 1:
            out.write("note: %s has %d product folders, so its rename is ignored.\n"
                      % (g.label, len(g.products)))
        # The user may deliberately file into a protected area (#RLV); the
        # plugin only allows that for rows that say so.
        decided = data["decisions"].get(g.key)
        # The user's own rename wins; "keep_names": true opts a brand out.
        keep = bool(decision and decision.get("rename")) or bool(decided and decided.get("keep_names"))
        auto = {} if keep or dest in new_folders else proposed_renames(tri, g, dest, folders_by_path)
        protected = bool(decision) and any(EXCLUDED_NAME_RE.search(part) for part in dest.split(SEP))
        for f, n, where in g.products:
            # The ids let the plugin that applies the plan move exactly this
            # folder: product names repeat (redeliveries).
            new_name = auto.get(f.id, "")
            because = how
            if new_name:
                because += "; renamed to match %s's naming" % dest.split(SEP)[-1]
            rows.append({"product_folder": f.name, "items": n, "from": where,
                         "to": dest, "because": because,
                         "creates_folder": "yes" if dest in new_folders else "",
                         "folder_id": str(f.id), "from_id": str(f.parent),
                         "rename_to": new_name or rename, "allow_protected": "yes" if protected else ""})
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        w.writeheader()
        w.writerows(rows)
    out.write("Plan written to %s: %d folder moves (%d items), %d new folders; "
              "%d product folders still undecided. Nothing has been moved.\n" % (
                  path, len(rows), sum(r["items"] for r in rows), len(new_folders), undecided))
    return rows


# --------------------------------------------------------------------------
# Action plans: item and folder actions (prune, docs, dedupe)
#
# A second plan format, for the jobs that touch items and trash
# rather than only moving product folders. One row per action:
#   kind          item_move | item_trash | folder_trash
#   item_name     the item's name (the folder's, for folder_trash)
#   asset_type    the export's asset type spelling ("Notecard"); "Folder" for folders
#   from, to      folder paths; "to" is blank for the two trash kinds
#   because       the rule that fired, for review
#   item_id       the item's id (the folder's, for folder_trash)
#   from_id       the id of the folder it sits in
# Everything is a proposal against the export it was made from; whatever applies
# it re-checks against the live inventory.

ACTION_FIELDS = ["kind", "item_name", "asset_type", "from", "to", "because", "item_id", "from_id"]
ACTION_KINDS = ("item_move", "item_trash", "folder_trash")

# The cache's asset type -> the export's spelling ("notecard" -> "Notecard").
EXPORT_ASSET_NAMES = {}
for _spelling, _cache_type in icr.EXPORT_ASSET_TYPES.items():
    EXPORT_ASSET_NAMES.setdefault(_cache_type, _spelling)
EXPORT_ASSET_NAMES["category"] = "Folder"


def action_row(kind, item_name, asset_type, source, dest, because, item_id, from_id):
    return {"kind": kind, "item_name": item_name,
            "asset_type": EXPORT_ASSET_NAMES.get(asset_type, asset_type),
            "from": source, "to": "" if kind != "item_move" else dest,
            "because": because, "item_id": str(item_id), "from_id": str(from_id)}


def write_action_plan(rows, path):
    """Write action rows as UTF-8 CSV with a BOM (like write_plan). Raises
    ValueError on a row the plugin could not apply: an unknown kind, a trash
    with a destination, a move without one."""
    for r in rows:
        if r["kind"] not in ACTION_KINDS:
            raise ValueError("unknown action kind %r" % r["kind"])
        if (r["kind"] == "item_move") == (not r.get("to")):
            raise ValueError("%s needs %s destination: %r" % (
                r["kind"], "a" if r["kind"] == "item_move" else "no", r["item_name"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=ACTION_FIELDS, restval="")
        w.writeheader()
        w.writerows(rows)
    return rows


# Where product folders worth tidying live. Items directly in a system folder
# (Landmarks, Textures, Notecards, Trash...) are never in scope, and neither are
# outfits or the Marketplace areas; #RLV product folders are.
ITEM_ZONES = {icr.USER_ZONE, "#RLV", "Received Items", "Objects", "Clothing", "Body Parts"}
# Staging and queue folders that are never touched, whatever is inside.
ITEM_PROTECTED_RE = re.compile(
    r"wardrobe import|fitting required|needs? fitting|redelivery|rlv transfer", re.IGNORECASE)
# What a folder holds when it is only vendor leftovers (or the user's own notes
# and landmarks): no product, so nothing is pruned or filed out of it.
LEFTOVER_TYPES = {"landmark", "notecard", "texture", "material", "callcard", "snapshot"}


def product_roots(inv):
    """{folder id: its product folder} for every product folder and part
    subfolder whose items an item plan may touch: in a user area (or #RLV),
    not a staging or fitting queue, and holding a real product (not only
    landmarks, notecards and textures)."""
    roots = {}
    for f in inv.folders.values():
        if inv.kind.get(f.id) not in ("product", "part") or f.ptype != "-1" or f.zone not in ITEM_ZONES:
            continue
        if any(ITEM_PROTECTED_RE.search(a.name) for a in [f] + list(inv.ancestors(f))):
            continue
        root, hops = f, 0
        while inv.kind.get(root.id) == "part" and hops < 4:
            root, hops = inv.folders.get(root.parent) or root, hops + 1
        if inv.kind.get(root.id) != "product" or set(inv.types(root)) <= LEFTOVER_TYPES:
            continue
        roots[f.id] = root
    return roots


def subtree(inv, f):
    yield f
    for c in f.children:
        for d in subtree(inv, c):
            yield d


def _words(text, size=4):
    return set(re.findall(r"[a-z]{%d,}" % size, text.lower()))


def clean_brand(name):
    """A brand as a folder name: no path separators, no leading dot or tilde."""
    name = re.sub(r"[/\\>]+", "-", name).strip().lstrip(" .~_-")
    return re.sub(r"\s+", " ", name)[:60].strip()


# Words that are what a thing is, not whose it is: never a brand key.
GENERIC_KEYS = set("bag mesh male female neck shape utilities unsorted".split())
PACKAGING_TAIL_RE = re.compile(r"[\s\-]+(boxed?|unpacked|unpacker|fatpack|demo)$", re.IGNORECASE)


def own_brand_key(name):
    """The brand key of a product's own name, or "": the triage's
    brand_key_text without packaging words ("Cloak BOXED") and without words
    that only say what the thing is ("Add Me", "Bag", "Hair"); "[Add Me] Squish
    Belt" is the brand of what follows the tag."""
    for text in (name, BRACKET_TAG_RE.sub("", name, count=1)):
        key = clean_brand(PACKAGING_TAIL_RE.sub("", brand_key_text(text)))
        words = set(norm(key).split())
        if words and not words <= (STOP_WORDS | TYPE_WORDS) and not PART_NAME_RE.match(key) \
                and squash(key) not in GENERIC_KEYS:
            return key
    return ""


def is_brand_folder(inv, f):
    """A folder the triage calls a brand whose products really carry its name
    (a "Glasses" folder of one creator's products is a type folder)."""
    if inv.kind.get(f.id) != "brand":
        return False
    own = squash(f.name)
    products = [c for c in f.children if inv.kind.get(c.id) == "product"]
    named = sum(1 for p in products
                if squash(p.name).startswith(own) or own == squash(brand_key_text(p.name)))
    return bool(own and products) and named >= 0.5 * len(products)


def product_brands(inv):
    """{product folder id: brand} the way the triage derives it: a product in a
    brand folder takes that folder's name; a product anywhere else (the flat
    Kink bucket, a category, the root) takes its own brand key; failing that,
    its main creator; last resort "Unsorted". One spelling per brand."""
    raw = {}
    for f in inv.folders.values():
        if inv.kind.get(f.id) != "product":
            continue
        parent = inv.folders.get(f.parent)
        name, weight = "", 1
        if parent is not None and is_brand_folder(inv, parent):
            name, weight = clean_brand(parent.name), 3
        if not name:
            name = own_brand_key(f.name)
        if not name:
            creator = inv.dominant_creator(f)
            full = inv.creator_names.get(creator, "") if creator else ""
            name = clean_brand(re.sub(r"\s+Resident$", "", full.split(" (")[0]))
        raw[f.id] = (name or "Unsorted", weight)
    # A two-word name with no separator ("TRV mats") is the brand of its first
    # word when another product carries that word as its brand (as in _collect).
    present = {squash(n) for n, _ in raw.values()}
    for fid, (name, weight) in list(raw.items()):
        words = name.split()
        head = squash(words[0]) if len(words) == 2 else ""
        if weight == 1 and len(head) >= 3 and head != squash(name) and head in present and head not in STOP_WORDS:
            raw[fid] = (words[0], weight)
    spelling = defaultdict(Counter)
    for name, weight in raw.values():
        spelling[squash(name) or name][name] += weight
    return {fid: spelling[squash(name) or name].most_common(1)[0][0] for fid, (name, _) in raw.items()}


# --- docs: the brand folder under Notecards > Docs ---------------------------

# Category / part / area words that are what a thing is (or where it was put),
# never whose it is. Added to PART_NAME_RE and the dedupe vocabulary.
DOCS_GENERIC_RE = re.compile(
    r"^[\W_]*(accessor(y|ies)|bod(y|ies)|collars?|hoods?|hosiery|shapes?|bottoms?|tops?|chokers?|"
    r"corsets?|dress(es)?|gadgets?|latex|lingerie|mittens?|rubber|straitjackets?|yokes?|"
    r"shoes?|boots?|heels?|catsuits?|outfits?|misc(ellaneous)?|others?|unsorted|docs?|documentation|"
    r"notecards?|boxed( items?)?|unpacked|prisoner suits?|add ?me|rootsort|restraints?|bdsm|"
    r"bdsm (and|&) restraints|furniture|utilities|cuffs|armbinders?|harnesses|gags?|"
    r"stockings|leggings|skirts?|gloves|hats?|bras?|belts?|masks?|suits?|"
    r"kink|e-?body( reborn)?|reborn|legacy|bento|evo-?x|(inventory |scripts? (and|&) )?tools?|scripts?|vision systems?)[\W_]*$",
    re.IGNORECASE)


def is_generic_brand(name):
    """A word for a kind of thing or a place to keep it, not a brand."""
    name = " ".join((name or "").split())
    return (not norm(name) or bool(PART_NAME_RE.match(name) or DEDUPE_GENERIC_RE.match(name)
                                   or DOCS_GENERIC_RE.match(name) or STAGING_NAME_RE.match(name))
            or squash(name) in GENERIC_KEYS or norm(name) in NOT_BRANDS)


def clean_creator_name(full):
    """A creator's name as a brand: no parenthesised part, no " Resident"."""
    full = re.sub(r"\s*\([^)]*\)?", "", full or "").strip()
    return clean_brand(re.sub(r"\s+Resident$", "", full, flags=re.IGNORECASE))


def _brand_map_lookup(brands):
    return {squash(k): v for k, v in (brands or {}).items() if squash(k)}


def docs_brand_home(inv, root):
    """The name of the brand folder the product sits in, or "": the nearest
    ancestor (up to the area root) that the triage calls a brand, or a folder
    below the area level whose contents are mostly by the product's creator
    (Marigold > Delilah, ~vendor > Prisoner Suit), unless its name is generic."""
    creator = inv.dominant_creator(root)
    for a in inv.ancestors(root):
        if inv.folders.get(a.parent) is inv.root or a.parent == inv.root.id:
            break                                   # an area folder itself
        name = clean_brand(a.name)
        if is_generic_brand(name):
            continue
        products = [c for c in a.children if inv.kind.get(c.id) == "product"]
        keys = Counter(squash(own_brand_key(re.sub(r"\s*\(\d+\)$", "", c.name))) for c in products)
        top, n = keys.most_common(1)[0] if keys else ("", 0)
        own_creator = squash(clean_creator_name(inv.creator_names.get(inv.dominant_creator(a), "")))
        if (top and top != squash(name) and n >= 0.6 * len(products) and not is_generic_brand(top)
                and not (squash(name) and squash(name) in own_creator)):
            continue        # its products say a different brand: "Complete Sets" of TRV
        if inv.kind.get(a.id) == "brand" or (creator and len(products) >= 2
                                             and inv.dominant_creator(a) == creator
                                             and inv.kind.get(a.id) in ("bucket", "category")):
            return name
    return ""


def docs_brands(inv, brands=None, roots=None, existing=()):
    """{folder id: the Notecards > Docs brand folder name} for every product
    folder and part folder of product_roots. In order:
    1. a brand folder above the product (docs_brand_home);
    2. the triage's brand key of the product's own name (or, when that is a
       generic container such as "Hoods", of the folder the notecard is in),
       through the map's "brands"; a two-word key that no other product shares
       is a product name, not a brand, unless it is a [Tag];
    3. (the store cache is keyed by brand key and holds no creator or store
       name, so there is no creator -> store lookup to use);
    4. the main creator's name, without " Resident" and parentheses;
    5. never a generic word (is_generic_brand), each rule falls through;
    6. one spelling for case / punctuation variants, an existing
       Notecards > Docs folder first, and the longer of "MOSAIC" / "MOSAIC.mods"
       when one creator made both."""
    mapped = _brand_map_lookup(brands)
    roots = roots if roots is not None else product_roots(inv)
    key_names = defaultdict(set)
    for f in inv.folders.values():
        if inv.kind.get(f.id) == "product":
            key_names[squash(own_brand_key(f.name))].add(squash(f.name))
    key_count = {k: len(v) for k, v in key_names.items()}

    def usable(name):
        name = clean_brand(re.sub(r"\s+Resident$", "", name or "", flags=re.IGNORECASE))
        name = mapped.get(squash(name), name)
        return "" if is_generic_brand(name) else name

    def key_of(folder, strict=False):
        """The brand key of a folder name. A two-word key is a brand only
        when it is a [Tag] or its first word is another product's brand ("TRV
        mats" is TRV); "Squish Belt - Cornered" is a product. `strict` (a part folder under a generic
        container) also wants the key to be a tag or shared by two products."""
        key = own_brand_key(folder.name)
        words = key.split()
        m = BRACKET_TAG_RE.match(folder.name)
        tagged = bool(m and squash(m.group(1)) == squash(key))
        if key and not tagged and len(words) > 1:
            head = squash(words[0])
            others = {n for n in key_names.get(head, ()) if not n.startswith(squash(key))}
            key = words[0] if len(words) == 2 and len(head) >= 3 and others and head not in STOP_WORDS else ""
        if strict and key and not tagged and key_count.get(squash(key), 0) < 2:
            key = ""
        return usable(key)

    raw, creators = {}, defaultdict(set)
    for fid, root in roots.items():
        f = inv.folders[fid]
        creator = inv.dominant_creator(root) or inv.dominant_creator(f)
        name = usable(docs_brand_home(inv, root)) or key_of(root)
        chain = []
        cur = f
        while not name and cur is not root and cur is not None:
            chain.append(cur)
            name = key_of(cur, strict=True)
            cur = inv.folders.get(cur.parent)
        if not name:
            note_by = Counter(it["creator"] for it in inv.item_rows[fid] if it["type"] == "notecard"
                              and it["creator"] in inv.creator_names)
            by = creator if creator in inv.creator_names else (note_by.most_common(1)[0][0] if note_by else None)
            name = usable(clean_creator_name(inv.creator_names.get(by, ""))) if by else ""
        raw[fid] = name or "Unsorted"
        creators[fid].update(it["creator"] for it in inv.item_rows[fid] if it["type"] == "notecard")
        if creator:
            creators[fid].add(creator)
    # One spelling per squashed name: an existing folder first, else the most common.
    count = Counter(raw.values())
    existing_by = {}
    for e in existing:
        existing_by.setdefault(squash(e), e)
    spelling = {}
    for name in raw.values():
        sq = squash(name) or name
        spelling.setdefault(sq, name)
        if existing_by.get(sq):
            spelling[sq] = existing_by[sq]
        elif count[name] > count[spelling[sq]]:
            spelling[sq] = name
    final = {fid: spelling[squash(n) or n] for fid, n in raw.items()}
    # "MOSAIC" and "MOSAIC.mods" by one creator: the longer name.
    who = defaultdict(set)
    for fid, n in final.items():
        who[n] |= creators[fid]
    names = sorted(set(final.values()) | set(existing), key=len)
    longer = {}
    for short in names:
        for long_ in names:
            if (len(long_) > len(short) and long_.casefold().startswith(short.casefold())
                    and not long_[len(short)].isalnum() and not long_[len(short)].isspace()
                    and (who[short] & who[long_] or long_ in existing) and short != "Unsorted"):
                longer[short] = long_
    return {fid: longer.get(n, n) for fid, n in final.items()}


# --- job 1: prune ----------------------------------------------------------

# A texture named like a vendor's advert. Marketing words are strong evidence;
# poster, picture and logo (and the 1x1 of a vendor picture) are also what
# decor products are made of, so they count only when the name shares nothing
# with the product's.
AD_STRONG_RE = re.compile(r"\b(ads?\d*|advert\w*|vendors?|promo\w*|banner|flyer|teaser)\b", re.IGNORECASE)
AD_WEAK_RE = re.compile(r"\b(posters?|pics?|pictures?|logo|billboard)\b|\b[1-4]x[1-4]\b", re.IGNORECASE)
# ...unless it also looks like a part of the product (a map, a mask, a pattern).
PART_TEXTURE_RE = re.compile(
    r"normal|specular|\bspec\b|bump|mask|alpha|\bao\b|\bmap\b|diffuse|colou?r|pattern|print|\bbom\b|"
    r"tattoo|skin|applier|decal|overlay|emissive|rough|metal|fabric|textur|\btex\b|material|swatch|"
    r"\bhud\b|button|icon|label|name ?plate|badge", re.IGNORECASE)
# A folder (or ancestor) named like this holds textures on purpose.
KIT_FOLDER_RE = re.compile(r"\bbom\b|applier|tattoo|skin|textur|material|\bkits?\b", re.IGNORECASE)
KIT_SUBFOLDER_RE = re.compile(r"textur|\btex\b|\bkits?\b|\bhuds?\b|applier|\bbom\b|material|skin|tattoo",
                              re.IGNORECASE)
PRUNE_TEXTURES_MAX = 2
PRUNE_LANDMARKS_MAX = 5     # more than this in one product is a places list, not a vendor's landmark


def ad_evidence(name, brand, landmarks, product=""):
    """(why, strong) for a texture that looks like a vendor ad, or None:
    positive evidence only. A marketing word the product's own name carries
    ("Ad Wall") is no evidence. Underscores separate words ("Cabinet_ad")."""
    name = name.replace("_", " ")
    m = AD_STRONG_RE.search(name)
    if m and not re.search(r"\b%s\b" % re.escape(m.group(0)), product.replace("_", " "), re.IGNORECASE):
        return "name says ad (%s)" % m.group(0), True
    m = AD_WEAK_RE.search(name)
    if m:
        return "name says ad (%s)" % m.group(0), False
    sq = squash(name)
    if len(sq) >= 2 and sq == squash(brand):
        return "named like the brand (%s)" % brand, False
    for lm in landmarks:
        lq = squash(lm)
        if len(sq) >= 4 and lq and (sq == lq or sq in lq):
            return "named like the store (landmark %s)" % lm, False
    return None


def prune_plan(inv, stats=None):
    """Job 1: product folders hold the product. Every Landmark in a
    product folder goes to Trash; a Texture goes only when it is plainly a
    vendor ad (see ad_evidence) in a folder with no sign of a texture kit.
    Notecards, scripts, sounds, animations and gestures stay. Anything unsure
    stays; stats (a Counter) says why each texture was kept."""
    stats = stats if stats is not None else Counter()
    brands = product_brands(inv)
    by_root = defaultdict(list)
    for fid, root in product_roots(inv).items():
        by_root[root.id].append(inv.folders[fid])
    rows = []
    for root_id, folders in sorted(by_root.items(), key=lambda kv: (inv.path(inv.folders[kv[0]]), str(kv[0]))):
        folders.sort(key=lambda d: (inv.path(d), str(d.id)))
        rows.extend(_prune_product(inv, inv.folders[root_id], folders, brands.get(root_id, "Unsorted"), stats))
    return rows


def _prune_product(inv, root, folders, brand, stats):
    rows = []
    landmarks = [(d, it) for d in folders for it in inv.item_rows[d.id] if it["type"] == "landmark"]
    textures = [(d, it) for d in folders for it in inv.item_rows[d.id] if it["type"] == "texture"]
    if landmarks and len(landmarks) <= PRUNE_LANDMARKS_MAX:
        for d, it in landmarks:
            rows.append(action_row("item_trash", it["name"], "landmark", inv.path(d), "",
                                   "landmark in a product folder", it["id"], d.id))
    elif landmarks:
        stats["landmarks kept: %d in one product (a places list?)" % len(landmarks)] += len(landmarks)
    if not textures:
        return rows
    n = len(textures)
    sub_names = [d.name for d in subtree(inv, root) if d is not root]
    if inv.types(root).get("material"):
        stats["textures kept: product has materials"] += n
    elif n > PRUNE_TEXTURES_MAX:
        stats["textures kept: more than %d in the folder" % PRUNE_TEXTURES_MAX] += n
    elif any(KIT_FOLDER_RE.search(a.name) for a in [root] + list(inv.ancestors(root))):
        stats["textures kept: kit word in the folder name"] += n
    elif any(KIT_SUBFOLDER_RE.search(s) for s in sub_names):
        stats["textures kept: Textures/Kit/HUD subfolder"] += n
    else:
        store_names = [it["name"] for _, it in landmarks]
        product_words = _words(root.name) - _words(brand)    # the brand is in every name
        for d, it in textures:
            found = ad_evidence(it["name"], brand, store_names, root.name)
            if found is None:
                stats["textures kept: no sign it is an ad"] += 1
            elif PART_TEXTURE_RE.search(it["name"]):
                stats["textures kept: ad-like name but looks like a product part"] += 1
            elif not found[1] and _words(it["name"]) & product_words:
                stats["textures kept: weak ad sign and shares a word with the product name"] += 1
            else:
                rows.append(action_row("item_trash", it["name"], "texture", inv.path(d), "",
                                       "ad texture: " + found[0], it["id"], d.id))
    return rows


# --- job 2: docs -----------------------------------------------------------

DOCS_ROOT = "Notecards > Docs"
AO_ANIMATIONS_MIN = 3   # a product with this many animations and a notecard may be an AO's config


def docs_plan(inv, stats=None, brands=None):
    """Job 2: every notecard in a product folder goes to
    Notecards > Docs > <Brand>, the brand being the one the triage derives for
    the product folder (see docs_brands; `brands` is the map's "brands"). Notecards already under Notecards stay. A second copy
    (same name and creator, the export carries no asset id or size to compare)
    of one going to the same brand folder, or already filed there, is
    proposed for Trash instead."""
    stats = stats if stats is not None else Counter()
    docs_root = [f for f in inv.folders.values() if inv.path(f) == DOCS_ROOT]
    existing_brands = [c.name for f in docs_root for c in f.children]
    brands = docs_brands(inv, brands, existing=existing_brands)
    existing = set()
    for f in inv.folders.values():
        if f.zone == "Notecards":
            path = inv.path(f)
            existing.update((path, it["name"].casefold(), it["creator"]) for it in inv.item_rows[f.id]
                            if it["type"] == "notecard")
    rows, seen = [], set()
    todo = []
    for fid, root in product_roots(inv).items():
        f = inv.folders[fid]
        notes = [it for it in inv.item_rows[fid] if it["type"] == "notecard"]
        if not notes:
            continue
        if inv.types(root).get("animatn", 0) >= AO_ANIMATIONS_MIN:
            stats["notecards kept: product holds %d+ animations (an AO's config?)" % AO_ANIMATIONS_MIN] += len(notes)
            continue
        dest = DOCS_ROOT + SEP + brands.get(fid, "Unsorted")
        todo.extend((dest, inv.path(f), it["name"].casefold(), it["id"], f, it) for it in notes)
    for dest, source, _, _, f, it in sorted(todo, key=lambda t: t[:4]):
        key = (dest, it["name"].casefold(), it["creator"])
        if key in existing or key in seen:
            rows.append(action_row("item_trash", it["name"], "notecard", source, "",
                                   "duplicate notecard (same name and creator already under %s; "
                                   "content not compared, the export has no asset id)" % dest,
                                   it["id"], f.id))
            continue
        seen.add(key)
        rows.append(action_row("item_move", it["name"], "notecard", source, dest,
                               "notecard in a product folder (docs)",
                               it["id"], f.id))
    return rows


# --- job 3: dedupe ---------------------------------------------------------

# Folders under these are never proposed for Trash (Trash itself, outfits, the
# inbox and #RLV); inv.excluded adds the fitting queues and anything "rlv".
# Groups are looked for in ITEM_ZONES only: #RLV and the inbox members are shown,
# never trashed.
DEDUPE_PROTECTED_ZONES = {"#RLV", "My Outfits", "Current Outfit", "Received Items", "Trash"}


def trash_protected(inv, f):
    return (f.ptype != "-1" or f.zone in DEDUPE_PROTECTED_ZONES or inv.excluded(f)
            or any(ITEM_PROTECTED_RE.search(a.name) for a in [f] + list(inv.ancestors(f))))


def folder_signature(inv, f):
    """The multiset of (item name, asset type) in a folder and everything below it."""
    sig = Counter((name, ty) for d in subtree(inv, f) for ty, _, name in inv.items[d.id])
    return frozenset(sig.items())


def is_brand_home(inv, f):
    """A product folder filed in a brand's own folder (or the flat Kink-style
    bucket) in Clothing, Objects or Body Parts, not in staging or a boxed area."""
    parent = inv.folders.get(f.parent)
    return (parent is not None and f.zone in ("Clothing", "Objects", "Body Parts")
            and inv.kind.get(parent.id) in ("brand", "bucket")
            and not inv.staging(f) and not Triage.boxed_path(inv.path(f)))


# Generic part / sub-folder vocabulary: never a product's own name, so never a
# dedupe candidate ("Eyes" under one head and "Eyes" in a box are different things).
DEDUPE_GENERIC_RE = re.compile(
    r"^[\W_]*(eyes?|heads?|shapes?|skins?|bod(y|ies)|hands?|feet|foot|fatpacks?|colou?rs?|"
    r"installers?|accessor(y|ies)|boots?|tops?|bottoms?|outers?|outfits?|shirts?|pants|skirts?|"
    r"dress(es)?|jackets?|lashes|brows?|eyebrows?|nails?|makeup|tattoos?|lips?|ears?|teeth|"
    r"tongues?|mouths?|piercings?|jewel(l)?ery|bags?|hats?|glasses|gloves|socks|tights|"
    r"stockings|sandals|heels|bikinis?|underwear|lingerie|bras?|panties|swimwear|"
    r"updates?|versions?|new|old|misc|stuff|files?|folders?|resources?|"
    r"mesh|male|female|unisex|bento|rigged|standard|classic|default|basic|full|lite|set)"
    r"([\W_]+(eyes?|heads?|shapes?|skins?|hud|huds|body|bodies|parts?|set|pack|kit|folder))?[\W_]*$",
    re.IGNORECASE)
BODY_PART_ANCESTOR_RE = re.compile(r"^Body Parts > (Bodies|Heads)(?= > |$)")


def dedupe_candidate(inv, f):
    """True for a product-root folder that may be grouped: a product, not a part
    subfolder, not inside another product, with a specific name (5+ letters and
    not only generic part words)."""
    if inv.kind.get(f.id) != "product" or f.ptype != "-1" or f.zone not in ITEM_ZONES:
        return False
    name = " ".join(f.name.split())
    if len(re.sub(r"[^A-Za-z]", "", name)) < 5:
        return False
    if PART_NAME_RE.match(name) or DEDUPE_GENERIC_RE.match(name):
        return False
    if any(inv.kind.get(a.id) in ("product", "part") for a in inv.ancestors(f)):
        return False
    return True


def default_trash_blocked(inv, f):
    """A folder that is never trashed by default: inside a Body Parts body or
    head folder, or inside another product folder."""
    return (any(inv.kind.get(a.id) in ("product", "part") for a in inv.ancestors(f))
            or bool(BODY_PART_ANCESTOR_RE.match(inv.path(f))))


def _folder_texts(inv, f):
    return [f.name] + [n for d in subtree(inv, f) for ty, _, n in inv.items[d.id]]         + [d.name for d in subtree(inv, f) if d is not f]


def plausible_pair(inv, box, opened):
    """The box and the opened folder look like one product: the box's object
    name(s) carry the opened folder's brand key, or share two 4+ letter words with
    the opened folder's items and subfolders."""
    box_objs = [n for ty, _, n in inv.items[box.id] if ty == "object"] or [box.name]
    key = squash(own_brand_key(opened.name))
    if key and any(key in squash(n) for n in box_objs):
        return True
    shared = _words(" ".join(box_objs)) & _words(" ".join(_folder_texts(inv, opened)[1:]))
    return len(shared - _words(" ".join(STOP_WORDS | TYPE_WORDS))) >= 2


def dedupe_groups(inv):
    """Job 3: groups of product folders with the same name (stripped,
    case-folded, spaces collapsed), each classified:
      box+opened  one is a box (BOXED/SINGLE by classify_box), another is opened
                  (UNPACKED): default, Trash the box (the Marketplace can redeliver)
      identical   the same item names and types: default, keep the copy filed in
                  a brand home, Trash the rest. Several copies in one brand home
                  folder (or, with none in a brand home, all in one folder) are
                  twins: keep the first. Copies in different brand homes need a look.
      versions    anything else: no default, review
    A group is a dict: n, kind, members, keep, trash, default (text). Folders
    that are never trashed (#RLV, outfits, Received Items, Trash, queues) stay
    members, so the review shows them, but are never in "trash"."""
    by_name = defaultdict(list)
    for f in inv.folders.values():
        if dedupe_candidate(inv, f):
            by_name[" ".join(f.name.split()).casefold()].append(f)
    groups = []
    for name, members in sorted(by_name.items()):
        if len(members) < 2:
            continue
        members.sort(key=lambda f: (inv.path(f), str(f.id)))
        kinds = {f.id: classify_box(inv, f)[0] for f in members}
        boxes = [f for f in members if kinds[f.id] in ("SINGLE", "BOXED")]
        opened = [f for f in members if kinds[f.id] == "UNPACKED"]
        g = {"n": 0, "kind": "versions", "members": members, "kinds": kinds,
             "keep": None, "trash": [], "default": "review: no rule decides it"}
        if boxes and opened:
            g["kind"] = "box+opened"
            g["trash"] = [f for f in boxes if not trash_protected(inv, f) and not default_trash_blocked(inv, f)]
            g["default"] = ("trash the box" + ("es" if len(boxes) > 1 else "")) if g["trash"] else \
                "review: the box is in a protected area"
            if g["trash"] and not any(plausible_pair(inv, b, o) for b in g["trash"] for o in opened):
                g["trash"] = []
                g["default"] = "review: the box and the opened folder may not be the same product"
        elif len({folder_signature(inv, f) for f in members}) == 1:
            g["kind"] = "identical"
            homes = [f for f in members if is_brand_home(inv, f)]
            keeper = None
            if homes and len({f.parent for f in homes}) == 1:
                keeper = homes[0]
            elif not homes and len({f.parent for f in members}) == 1:
                keeper = members[0]
            if keeper is not None:
                g["keep"] = keeper
                g["trash"] = [f for f in members if f is not keeper and not trash_protected(inv, f)
                              and not default_trash_blocked(inv, f)]
                g["default"] = ("keep the %s copy, trash %d other%s" % (
                    "brand-home" if homes else "first", len(g["trash"]), "" if len(g["trash"]) == 1 else "s")) \
                    if g["trash"] else "review: the other copies are in protected areas"
            else:
                g["default"] = "review: %s" % ("copies in different folders, none in a brand home" if not homes
                                              else "copies in %d different brand homes" % len({f.parent for f in homes}))
        groups.append(g)
    groups.sort(key=lambda g: (g["kind"] != "box+opened", g["kind"] != "identical", g["members"][0].name.casefold()))
    for n, g in enumerate(groups, 1):
        g["n"] = n
    return groups


def write_dedupe_review(inv, groups, path):
    """The review list, one line per group: n, kind, default, then every folder
    with its path, item count, class and what the default does to it."""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["n", "kind", "default", "folders (path [items, class] -> action)"])
        for g in groups:
            cells = []
            for f in g["members"]:
                action = "trash" if f in g["trash"] else "keep" if f is g["keep"] else \
                    ("protected" if trash_protected(inv, f) else "")
                cells.append("%s [%d items, %s]%s" % (inv.path(f), inv.count(f), g["kinds"][f.id],
                                                      " -> " + action if action else ""))
            w.writerow([g["n"], g["kind"], g["default"], " || ".join(cells)])


def dedupe_plan(inv, groups):
    """The rule-decided rows only: a folder_trash for each folder a group's
    default trashes."""
    rows = []
    for g in groups:
        for f in g["trash"]:
            rows.append(action_row(
                "folder_trash", f.name, "category", inv.path(inv.folders.get(f.parent)) or "(root)", "",
                "%s: %s" % (g["kind"], "a box, the opened copy is kept" if g["kind"] == "box+opened"
                            else "identical to %s, kept" % inv.path(g["keep"])),
                f.id, f.parent))
    return rows


# --- empty folders ----------------------------------------------------------

def _empties_blocked(f):
    """A folder an empties plan never trashes, nor anything inside it: system folders,
    vendor-managed ~ / .~ folders, redelivery folders."""
    n = f.name.strip()
    return (f.ptype != "-1" or n.startswith("~") or n.startswith(".~")
            or bool(re.search(r"redelivery", f.name, re.IGNORECASE)))


def empties_plan(inv, under=("#RLV",)):
    """folder_trash rows for the topmost folder of each subtree under the given
    roots (folder paths, e.g. "#RLV") that is empty recursively: no items or links
    anywhere, and no ~ / system folder inside it. The roots themselves are never
    listed."""
    wanted = set(under)
    memo = {}

    def trashable(f):
        if f.id not in memo:
            memo[f.id] = (not _empties_blocked(f) and not inv.items[f.id]
                          and f.id not in inv.link_parents
                          and all(trashable(c) for c in f.children))
        return memo[f.id]

    rows = []

    def walk(f):
        for c in sorted(f.children, key=lambda x: x.name.casefold()):
            if _empties_blocked(c):
                continue
            if trashable(c):
                rows.append(action_row("folder_trash", c.name, "category", inv.path(f), "",
                                       "empty folder (no items below)", c.id, f.id))
            else:
                walk(c)

    for f in sorted(inv.folders.values(), key=lambda x: inv.path(x)):
        if f is not inv.root and inv.path(f) in wanted and not _empties_blocked_chain(inv, f):
            walk(f)
    return rows


def _empties_blocked_chain(inv, f):
    """True if a root sits inside a ~ folder, a redelivery folder or the Trash."""
    return any((_empties_blocked(a) and a.ptype == "-1") or a.zone == "Trash"
               for a in [f] + list(inv.ancestors(f))) or f.zone == "Trash"


# --- job 4: RLV migration ---------------------------------------------------
#
# Folder plan (PLAN_FIELDS): every product folder in scope moves whole to its
# #RLV home and is renamed to a short name, because CTS Wardrobe makes one item
# per folder at a fixed depth and shows the folder name as the item's title:
#   #RLV > Restraints  > <Type> > <Short name>
#   #RLV > Clothes     > <Type> > <Short name>
#   #RLV > Accessories > <Type> > <Short name>
#   #RLV > Utilities   > <Short name>
# Folders whose name starts with ~ (vendor-managed, "~vendor") are never touched.

RLV_ROOT = "#RLV"
RLV_LAYOUT = {
    "Restraints": ["Armbinder", "Ballet Boots", "Blindfold", "Chastity Belt", "Collar", "Corset", "Cuffs - Arms",
                   "Cuffs - Legs", "Gag", "Harness", "Hood", "Leash", "Mittens", "Nose Hook", "Straitjacket",
                   "Tights", "Yoke", "Plug & Toy", "Other"],
    "Clothes": ["Catsuit", "Bodysuit", "Dress", "Top", "Bottom", "Lingerie", "Hosiery", "Shoes", "Boots",
                "Outfit", "Jacket", "Swimwear", "Other"],
    "Accessories": ["Piercing", "Jewellery", "Hat", "Glasses", "Gloves", "Handheld", "Other"],
}
RLV_UTILITIES = "Utilities"
# Where product folders are looked for (path prefixes; a folder equal to or below one).
RLV_SCOPE = ["Clothing", "Objects > BDSM & Restraints", "Objects > Kink",
             "Wardrobe Import", "Body Parts > Accessories", RLV_ROOT]
RLV_NAME_MAX = 60
RLV_LOOSE_MIN = 6      # items in a folder typed only from item names before it counts as several products


def _rl(rx):
    return re.compile(rx, re.IGNORECASE)


# Keyword rules, per area, in the order they are listed; a name is checked against all of them.
RLV_RULES = [
    ("Restraints", "Armbinder", _rl(r"armbinder|arm[\s-]binder")),
    ("Restraints", "Ballet Boots", _rl(r"\bballet(?![\s-]+(?:flats?|warm|cardigan|dress|top|skirt|tutu|leotard|wrap|"
                                       r"socks|tights|bodysuit|shrug))|ballerina[\s-]+boots?")),
    ("Restraints", "Blindfold", _rl(r"blindfold")),
    ("Restraints", "Chastity Belt", _rl(r"chastity")),
    ("Restraints", "Collar", _rl(r"collar|\bchokers?\b|\bposture\b")),
    ("Restraints", "Corset", _rl(r"corset")),
    ("Restraints", "Cuffs", _rl(r"cuff|shackle|manacle")),          # becomes Cuffs - Arms / - Legs
    ("Restraints", "Nose Hook", _rl(r"nose[\s-]?hook")),
    ("Restraints", "Gag", _rl(r"\bgags?\b|\bgagged\b")),
    ("Restraints", "Harness", _rl(r"harness")),
    ("Restraints", "Hood", _rl(r"\bhoods?\b|\bmasks?\b|gasmask|helmet|\bmuzzles?\b")),
    ("Restraints", "Leash", _rl(r"leash")),
    ("Restraints", "Mittens", _rl(r"mitten")),
    ("Restraints", "Straitjacket", _rl(r"strait[\s-]?jacket")),
    ("Restraints", "Yoke", _rl(r"\byokes?\b|fiddle")),
    ("Restraints", "Plug & Toy", _rl(r"\bplugs?\b|dildo|vibrator|\bvibes?\b|\bclamps?\b|\btoys?\b")),
    ("Clothes", "Catsuit", _rl(r"catsuit|cat[\s-]suit|zentai")),
    ("Clothes", "Bodysuit", _rl(r"bodysuit|body[\s-]suit|leotard|unitard|romper|playsuit|jumpsuit|onesie|kigurumi")),
    ("Clothes", "Swimwear", _rl(r"bikini|swim|monokini|tankini")),
    ("Clothes", "Dress", _rl(r"\b(?!head|ad)\w*dress(?:es)?\b|\bgowns?\b|\bkimonos?\b")),
    ("Clothes", "Top", _rl(r"\btops?\b|\btees?\b|shirt|blouse|sweater|hoodie|jumper|cardigan|\btanks?\b|"
                           r"\bcamis?\b|\bcrop\b|halter|\bvests?\b|turtleneck|pullover|tunic|bandeau|\bsleeves?\b")),
    ("Clothes", "Bottom", _rl(r"pants\b|shorts\b|\bjeans\b|leggings|\bbottoms?\b|trousers|skirt|"
                              r"joggers|culottes|capris|sweatpants|hotpants|\bkilts?\b|\btutu|chaps")),
    ("Clothes", "Lingerie", _rl(r"lingerie|panties|panty|\bthongs?\b|bralette|\bbras?\b|g-?string|\bteddy\b|"
                               r"babydoll|negligee|nightie|nightgown|underwear|knickers|garter|bustier|"
                               r"\bslips?\b|bloomers|sleepwear|pyjamas?|pajamas?|nightwear|chemise|bodystocking")),
    ("Clothes", "Hosiery", _rl(r"stocking|hosiery|\bsocks?\b|pantyhose|nylons|thigh[\s-]?highs?|leg[\s-]?warmers")),
    ("Clothes", "Shoes", _rl(r"\bshoes?\b|\bheels?\b|sandals?|\bflats\b|sneakers?|\bpumps\b|loafers?|slippers?|"
                            r"stilettos?|wedges|\bplatforms?\b|\bmules?\b|oxfords?|footwear")),
    ("Clothes", "Boots", _rl(r"\bboots?\b|booties|bootie")),
    ("Clothes", "Jacket", _rl(r"\bjackets?\b|\bcoats?\b|blazer|\bcapes?\b|cloak|parka|poncho|shrug|bolero|\brobes?\b")),
    ("Accessories", "Piercing", _rl(r"piercing|pasties|pastie|nipple|barbell|labret|septum|navel|"
                                    r"belly[\s-]?ring|nose[\s-]?ring|\bstuds?\b")),
    ("Accessories", "Jewellery", _rl(r"necklace|\brings?\b|earring|bracelet|anklet|pendant|jewel|bangle|"
                                    r"tiara|crown|brooch|\bpearls?\b")),
    ("Accessories", "Hat", _rl(r"\bhats?\b|\bcaps?\b|beanie|bonnet|beret|\bveil\b")),
    ("Accessories", "Glasses", _rl(r"glasses|goggle|sunglass|\bshades\b|monocle|eyewear")),
    ("Accessories", "Gloves", _rl(r"gloves?\b|gauntlet|fingerless")),
    ("Accessories", "Handheld", _rl(r"handheld|umbrella|\bbags?\b|backpack|purse|handbag|\bphone\b|\bwand\b|"
                                   r"\bfan\b|parasol")),
    ("Accessories", "Other", _rl(r"tattoo|\bwings?\b|\btails?\b|\bears\b|headband|scarf|scarves|armou?r|headphones?|headset|earmuffs?|\bribbons?\b|\bbows?\b|\bbib\b")),
]
# Several of these in one name make a set (or an outfit).
RLV_OUTFIT_RE = _rl(r"outfit|costume|uniform|ensemble")
RLV_SET_RE = _rl(r"\b(set|pack|bundle|collection)\b|[&+]|\band\b|\bwith\b")
RLV_LEG_RE = _rl(r"ankle|thigh|\blegs?\b|knee|calf|\bshins?\b")
RLV_ARM_RE = _rl(r"wrist|\barms?\b|upper[\s-]?arm|elbow|forearm|bicep")
RLV_TIGHTS_RE = _rl(r"\btights\b")
RLV_BELT_RE = _rl(r"\bbelts?\b")
RLV_RESTRAINT_CONTEXT_RE = _rl(r"kink|bdsm|restraint|fetish|mkr|latex|rubber")
RLV_UTILITY_RE = _rl(r"\brelays?\b|\bwhim\b|\bnodders?\b|\brlva?\b|\brlv\b")
RLV_VENDOR_RE = _rl(r"^[\s.]*~")
# What an object that is only a HUD, applier or unpacker is called.
RLV_HUDISH_RE = _rl(r"\bhuds?\b|applier|unpack|installer|updater|\bbom\b|textures?|\bmenu\b|colou?r changer|\bscripts?\b")
RLV_BODYPART_RE = _rl(r"^[\W_]*(avatar|hair|skins?|shapes?|heads?|eyes|bodies|body|eyebrows?|brows|lashes)[\W_]*$")

# Marketplace category (the deepest segment that says something wins) -> (area, type, weak).
# type None: the area is known, the type is not. Weak ones give way to a precise name keyword.
RLV_CATEGORIES = [
    (_rl(r"BDSM Collars"), "Restraints", "Collar", False),
    (_rl(r"BDSM Cuffs|Leashes & Restraints"), "Restraints", None, False),
    (_rl(r"BDSM Accessories"), "Restraints", None, False),
    (_rl(r"Corset"), "Restraints", "Corset", False),
    (_rl(r"\bMasks\b"), "Restraints", "Hood", False),
    (_rl(r"Mesh Bodies|Mesh Heads|Avatar Skins|Avatar Shapes|\bEyes\b|^Hair\b|\bHair$|Hair Styles|"
         r"Long Hair|Short Hair|Updo|Flexi Hair|Other Hair|Genitalia"), "OUT", None, False),
    (_rl(r"Catsuit"), "Clothes", "Catsuit", False),
    (_rl(r"Dresses|Gowns"), "Clothes", "Dress", False),
    (_rl(r"Skirts|Pants|Shorts|Jeans|Bottoms|Leggings"), "Clothes", "Bottom", False),
    (_rl(r"Tops|Shirts|Sweaters|T-shirts|Blouses|Tanks|Halters|Tube Tops|Vests|Camis"), "Clothes", "Top", False),
    (_rl(r"Stockings|Hosiery"), "Clothes", "Hosiery", False),
    (_rl(r"Lingerie|Underwear|Underpants|Bras\b|Garters|Intimates|Sleepwear|Teddies|Panties"),
     "Clothes", "Lingerie", False),
    (_rl(r"Swimwear|Bikinis|Swim "), "Clothes", "Swimwear", False),
    (_rl(r"Boots"), "Clothes", "Boots", False),
    (_rl(r"Heels|Sandals|Sneakers|Slippers|Mary Jane|Wedges|Stiletto|Mule|Pumps|Platform|Flats|Casual Shoes|"
         r"Dress Shoes"), "Clothes", "Shoes", False),
    (_rl(r"Footwear"), "Clothes", "Shoes", True),
    (_rl(r"Jackets|Coats|Outerwear"), "Clothes", "Jacket", False),
    (_rl(r"Outfits|Costumes|Uniforms|Ensembles|Apparel"), "Clothes", "Outfit", True),
    (_rl(r"Nose Rings and Piercing|Belly Jewelry|Piercing"), "Accessories", "Piercing", False),
    (_rl(r"Jewelry|Necklaces|Earrings|\bRings\b|Bracelets|Anklets|Crowns|Tiaras|Watches"),
     "Accessories", "Jewellery", False),
    (_rl(r"Hats|Scarves"), "Accessories", "Hat", False),
    (_rl(r"Gloves"), "Accessories", "Gloves", False),
    (_rl(r"Glasses"), "Accessories", "Glasses", False),
    (_rl(r"Handbags|Backpacks"), "Accessories", "Handheld", False),
    (_rl(r"Belts|Armor|Wings|Other Accessories|Hair Accessories|Furry Accessories"),
     "Accessories", "Other", True),
]


def rlv_category(category):
    """(area, type, weak) for a Marketplace category path (list of names), or None.
    area "OUT" means a body part (out of scope)."""
    for seg in reversed(category):
        for rx, area, kind, weak in RLV_CATEGORIES:
            if rx.search(seg):
                return area, kind, weak
    return None


def rlv_keyword_types(text, restraint_context=False):
    """[(area, type, position)] of the keyword rules that fire on a text, by position."""
    found = {}
    for area, kind, rx in RLV_RULES:
        m = rx.search(text)
        if m:
            found[(area, kind)] = m.start()
    if ("Restraints", "Cuffs") in found:
        pos = found.pop(("Restraints", "Cuffs"))
        found[("Restraints", "Cuffs - Legs" if RLV_LEG_RE.search(text) and not RLV_ARM_RE.search(text)
               else "Cuffs - Arms")] = pos
    t = RLV_TIGHTS_RE.search(text)
    if t:
        if restraint_context or RLV_RESTRAINT_CONTEXT_RE.search(text):
            found[("Restraints", "Tights")] = t.start()
        else:
            found[("Clothes", "Hosiery")] = t.start()
    b = RLV_BELT_RE.search(text)
    if b and ("Clothes", "Lingerie") not in found:      # garter belt is lingerie
        if restraint_context or ("Restraints", "Chastity Belt") in found:
            found[("Restraints", "Chastity Belt")] = b.start()
        else:
            found[("Accessories", "Other")] = b.start()
    return sorted(((a, k, p) for (a, k), p in found.items()), key=lambda x: x[2])


def rlv_name_type(text, restraint_context=False):
    """Decide a type from one text: (area, type), ("MULTI", "A, B"), or None.
    Restraints win over clothes, clothes over accessories. Several types in one
    name are a set (Outfits for clothes) unless one word is plainly the noun."""
    hits = rlv_keyword_types(text, restraint_context)
    for area in ("Restraints", "Clothes", "Accessories"):
        mine = [h for h in hits if h[0] == area]
        if not mine:
            continue
        if area == "Clothes" and RLV_OUTFIT_RE.search(text):
            return "Clothes", "Outfit"
        types = [h[1] for h in mine]
        if area == "Accessories" and "Piercing" in types:
            return area, "Piercing"                 # "Nipple Rings"
        if len(types) == 1:
            return area, types[0]
        if RLV_SET_RE.search(text):
            if area == "Clothes":
                return "Clothes", "Outfit"
            return "MULTI", ", ".join(types)
        return area, mine[-1][1]                # the last word is the noun: "Latex Hood"
    if RLV_OUTFIT_RE.search(text) or re.search(r"\bset\b", text, re.IGNORECASE):
        return "Clothes", "Outfit"
    return None


# --- short names -------------------------------------------------------------

RLV_BODY_RE = _rl(r"\b(maitreya|lara\s*x?|laurax?|petite|legacy|perky|reborn|e-?body|freya|isis|venus|"
                  r"hourglass|kupra|gianni|jake|belleza|slink|kemono|erika|waifu|peach|physique|"
                  r"inithium|altamura|geralt)\b")
RLV_NOISE_RES = [_rl(rx) for rx in (
    r"[\(\[\{]\s*(?:un)?boxed\s*[\)\]\}]", r"[\(\[\{]\s*box\s*[\)\]\}]", r"\b(?:un)?boxed\b", r"\bbox\b",
    r"\b(?:wear|rez|add|touch|click)(?: me)? to unpack\b", r"\bwear me\b", r"\badd me\b", r"\brez me\b",
    r"\bunpacker\b", r"\bunpacked\b", r"\bbom\b", r"\bappliers?\b", r"\bhuds?\b", r"\bdemo\b",
    r"\bfat[\s-]?packs?\b", r"\bmega[\s-]?packs?\b", r"\bfull[\s-]?packs?\b", r"\ball colou?rs\b",
    r"\bpacks?\b", r"[\(\[\{]\s*f\s*[\)\]\}]", r"\bfemale\b")]
RLV_ADD_RE = re.compile(r"\bADD\b")                  # the vendor's shout, not the word
RLV_VERSION_RE = _rl(r"\bv\.?\s?\d+(?:\.\d+)*[a-z]?\b|\brev\.?\s?\d+\b|\bversion\s*\d+(?:\.\d+)*\b")
RLV_LEAD_TAG_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]|\{[^}]*\}|<[^>]*>|\([^)]{0,20}\)|::[^:]*::|[*~=|]+[^*~=|]{1,30}[*~=|]+)(?![A-Za-z0-9])\s*[-:|~.]*\s*")
RLV_EDGE = " \t-:|/\\.~,;!&+*_=<>#"
# Words that do not make a name: what a thing is, what it is made of, filler.
RLV_GENERIC_WORDS = set(
    "the a an and of for with set pack outfit latex rubber leather classic basic new old my mini long short "
    "thigh ankle wrist elbow head black white red blue pink gold silver metal chrome lined pair female male "
    "unisex mesh wearable restraint restraints belt shock lock locking bondage fetish bdsm "
    "grey gray green yellow purple brown maroon nude beige cream ivory navy teal orange violet clear opaque sheer "
    "transparent mint pastel ruby noir rust snow taupe onyx ink dark light tan khaki lavender lilac".split())


def _informative(text):
    """True if a text has a word that is not a kind of thing, a material or filler."""
    for w in re.findall(r"[a-z0-9']+", text.lower()):
        w = w.strip("'")
        if (w.isdigit() or len(w) < 2 or w in RLV_GENERIC_WORDS or w.rstrip("s") in RLV_GENERIC_WORDS
                or w in RLV_PART_WORDS):
            continue
        if rlv_keyword_types(w) or RLV_OUTFIT_RE.search(w):
            continue
        return True
    return False


def _brand_rx(brand):
    words = re.findall(r"[a-z0-9]+", brand.lower())
    if not words or len("".join(words)) < 2:
        return None
    return r"\W*".join(re.escape(w) for w in words)


def _unbracket(s):
    """Drop brackets at the ends that have no partner."""
    for open_, close in ("()", "[]", "{}"):
        while s.startswith(open_) and s.count(open_) > s.count(close) or s.startswith(close):
            s = s[1:].lstrip(RLV_EDGE)
        while s.endswith(open_) or (s.endswith(close) and s.count(close) > s.count(open_)):
            s = s[:-1].rstrip(RLV_EDGE)
    return s


RLV_BRACKET_NOISE_RE = _rl(r"[\(\[\{]\s*(?:add|wear|wear me|add me|unpack me|unpack|rez|rez me|copy|bxd)\s*[\)\]\}]")
RLV_MORE_NOISE_RES = [_rl(rx) for rx in (
    r"\bunpack me\b", r"\bbxd\b", r"\bhud[\s-]?pack\b", r"\bhuds\b", r"\bfull[\s-]?perm\b")]
RLV_DANGLING_RE = _rl(r"[\s,]+(?:for|with|and|by|in|on|of|the|a|an|to)$")
RLV_OPEN_TAG_RE = re.compile(r"^[\[\{\(<]\s*([^\]\}\)>]*?)\s*[\]\}\)>]\s*")


def _rlv_clean(name, brands, strip_brand, strip_body, keep_version):
    s = " " + name.replace("_", " ") + " "
    s = re.sub(r"[\[\(\{]([A-Za-z0-9])[\]\)\}](?=[A-Za-z])", r"\1", s)       # "[H]arsh" is Harsh
    s = re.sub(r"\s-{1,}\s|\s-{1,}(?=\s*$)|^\s*-{1,}\s|-{2,}", " ", s)       # " - " and "--" separate; "T-shirt" stays
    s = re.sub(r"\s[/\\~|]+\s|[<>]{2,}", " ", s)                              # a spaced slash or tilde separates too
    s = re.sub(r"\b([A-Za-z])/([A-Za-z])(?:/([A-Za-z]))?\b", lambda m: "".join(g for g in m.groups() if g), s)  # A/D/E
    s = s.strip()

    def strip_tags(s):
        while True:
            m = RLV_LEAD_TAG_RE.match(s)
            if not m or not s[m.end():].strip(RLV_EDGE):
                return s
            s = s[m.end():]

    s = strip_tags(s)
    if strip_brand:
        for brand in sorted(brands, key=len, reverse=True):
            rx = _brand_rx(brand)
            if not rx:
                continue
            m = re.match(r"^\W*(?:%s)(?![a-z0-9])" % rx, s, re.IGNORECASE)
            if m and s[m.end():].strip(RLV_EDGE):
                s = s[m.end():].lstrip(RLV_EDGE + " ")
                t = RLV_OPEN_TAG_RE.match(s)            # a tag right after the brand is the product's name
                if t and t.group(1).strip():
                    s = t.group(1).strip() + " " + s[t.end():]
            m = re.search(r"[\s\-:|/~(]+(?:by\s+)?(?:%s)\)?\s*$" % rx, s, re.IGNORECASE)
            if m and s[:m.start()].strip(RLV_EDGE):
                s = s[:m.start()]
    else:
        s = re.sub(r"^\s*(?:::)", "", s)
    s = re.sub(r"::\s*([^:]*?)\s*::", r" \1 ", s)
    for _ in range(2):
        s = RLV_BRACKET_NOISE_RE.sub(" ", s)
        for rx in RLV_NOISE_RES + RLV_MORE_NOISE_RES:
            s = rx.sub(" ", s)
        s = RLV_ADD_RE.sub(" ", s)
        if not keep_version:
            s = RLV_VERSION_RE.sub(" ", s)
        if strip_body:
            s = RLV_BODY_RE.sub(" ", s)
        s = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", s)                  # brackets emptied by the above
        s = re.sub(r"(?<=\w)-(?=\s|$)|(?<=\s)-(?=\w)|(?<=\s)-(?=\s)", " ", s)     # hyphens left dangling
    s = re.sub(r"\s*[\(\[\{]\s*[-:|.,/\\]*\s*([^)\]}]*?)\s*[-:|.,/\\]*\s*[\)\]\}]",
               lambda m: " (%s)" % m.group(1).strip()
               if (len(re.findall(r"[A-Za-z]", m.group(1))) > 2 or re.search(r"\d", m.group(1))) else " ", s)
    s = re.sub(r"\s+", " ", s).strip(RLV_EDGE)
    while True:
        t = RLV_DANGLING_RE.sub("", s).strip(RLV_EDGE)
        if t == s or not t:
            break
        s = t
    s = s.replace("/", " and ").replace("\\", " and ")
    s = re.sub(r"\s+", " ", s).strip(RLV_EDGE)
    s = re.sub(r"\s+([,.;:!])", r"\1", s)
    s = re.sub(r"\s*:{2,}\s*", " ", s).strip(RLV_EDGE)
    whole = re.fullmatch(r"[\(\[\{]([^()\[\]{}]*)[\)\]\}]", s)             # a name that is only a tag
    return _unbracket(whole.group(1).strip() if whole and whole.group(1).strip() else s)


RLV_SMALL_WORDS = {"and", "of", "the", "for", "with", "in", "on", "a", "an", "by", "to"}


def rlv_short_name(name, brands=(), keep_version=False, keep_body=False):
    """The Wardrobe title for a product folder: its own name without the brand, tags,
    body names, sizes, packaging words and versions. A step that would leave nothing but
    type words ("Tights") is not taken, so "Desire Tights" keeps its brand."""
    brands = [b for b in brands if b]
    out = ""
    steps = ((True, True), (False, True), (True, False), (False, False))
    for strip_brand, strip_body in (steps[2:] if keep_body else steps):
        out = _rlv_clean(name, brands, strip_brand, strip_body, keep_version)
        if out and _informative(out):
            break
    if not out:
        out = re.sub(r"\s+", " ", name.replace("/", " and ").replace("\\", " and ")).strip(RLV_EDGE)
    out = out.lstrip(". ~")
    letters = [c for c in out if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 4:     # "MKR", "FACS" are names
        out = re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), out)
    else:                       # a lower-case word gets its capital; "WhimsiCuffs", "eBody", "NRR" are left
        out = re.sub(r"(?<![A-Za-z0-9'])([a-z])([a-z']*)(?!\d)",
                     lambda m: m.group(0) if m.group(0) in RLV_SMALL_WORDS and m.start() > 0
                     else m.group(1).upper() + m.group(2), out)
    if len(out) > RLV_NAME_MAX:
        out = out[:RLV_NAME_MAX].rsplit(" ", 1)[0] if " " in out[:RLV_NAME_MAX] else out[:RLV_NAME_MAX]
    return _unbracket(out.rstrip(RLV_EDGE)) or name.strip()[:RLV_NAME_MAX]


def rlv_canonical_dest(path):
    """True for a destination path the layout defines: #RLV > Utilities, or #RLV > Area > Type."""
    parts = path.split(SEP)
    if parts == [RLV_ROOT, RLV_UTILITIES]:
        return True
    return len(parts) == 3 and parts[0] == RLV_ROOT and parts[2] in RLV_LAYOUT.get(parts[1], ())


def rlv_dest(area, kind):
    return SEP.join([RLV_ROOT, RLV_UTILITIES] if area == RLV_UTILITIES else [RLV_ROOT, area, kind])


# --- finding the product folders ----------------------------------------------

RLV_PART_WORDS = set("hud huds legacy docs doc notes notecards notecard alpha alphas applier appliers bom "
                     "demo demos color colors colour colours rigged fitted standard not "
                     "extras extra unpack box options option textures texture materials material "
                     "left right upper lower forearm forearms arm arms leg legs ankle ankles wrist wrists "
                     "thigh thighs accessories accessory addons addon key keys emergency chain chains "
                     "targets backup main torso waist front rear back base small medium large tall slim thin "
                     "curvy regular xs xl xxl chest hips hip neck hand hands feet foot".split())
# Only parts when the parent is a set ("Sports Set - Taylor > Taylor - Top - Legacy").
RLV_SET_PART_WORDS = set("top tops bottom bottoms bra panties".split())
RLV_SET_PARENT_RE = _rl(r"\b(set|outfit|bikini|pack|kit|suit|lingerie|sets)\b")
# Folders where the user built the layout by hand: what is below is walked by the unit rule.
RLV_HAND_ZONES = (RLV_ROOT, "Wardrobe Import")


def _tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def rlv_partish(child, parent):
    """A subfolder named like a part of its parent ("Lucy - HUD", "Freya Vixen - Ankles")."""
    if PART_NAME_RE.match(child.name):
        return True
    own = set(_tokens(parent.name))
    rest = [t for t in _tokens(child.name) if t not in own and not t.isdigit()]
    rest = [t for t in rest if not RLV_BODY_RE.fullmatch(t)]
    if not rest:
        return True
    words = RLV_PART_WORDS | (RLV_SET_PART_WORDS if RLV_SET_PARENT_RE.search(parent.name) else set())
    return any(t in words for t in rest)


def rlv_container_named(name):
    """A folder named for a kind of thing ("Collars", ".Collars", "Clothing"), not a product."""
    plain = name.strip(" .~_-")
    if not plain:
        return True
    if DOCS_GENERIC_RE.match(plain) or re.match(
            r"^(other|misc|various|assorted|more|extra)( (brands?|stuff|items|things|restraints|clothes))?$",
            plain, re.IGNORECASE) or re.match(
            r"^(clothing|clothes|accessories|fetish|restraints|toys|outfits|footwear|kink|gadgets|import|"
            r"wardrobe import)$", plain, re.IGNORECASE):
        return True
    return bool(rlv_keyword_types(plain)) and len(plain.split()) <= 3 and not _informative(plain)


def rlv_is_unit(inv, c):
    """A folder that is one wearable thing, not a group of them: it holds items itself, or
    most of its subfolders are parts of it (HUDs, Ankles, Alpha, Legacy). A folder of
    products, or a wrapper around one ("SOLE" > "SOLE - Backpack"), is a group."""
    kids = [k for k in c.children if k.ptype == "-1"]
    if inv.items[c.id]:
        return True
    if not kids:
        return False
    parts = sum(1 for k in kids if rlv_partish(k, c))
    return parts == len(kids) or (parts >= 2 and 3 * parts >= len(kids))


def rlv_roots(inv, stats=None):
    """[(root folder, why)] the product folders to file: the planner's own walk.
    Outside #RLV and Wardrobe Import the triage's classification decides: a product is one
    (unless it is named for a kind of thing and holds folders: then its folders are); a brand
    or bucket whose subfolders are mostly parts of one thing is one; anything else is walked
    into. In those two hand-built zones a folder is an item when rlv_is_unit says so (a
    vendor's group of products is walked into), and what is directly in a folder named for a
    kind of thing (Hoods, Clothing, Utilities) is looked at first."""
    stats = stats if stats is not None else Counter()
    roots, seen = [], set()

    def add(f, why=""):
        if f.id not in seen:
            seen.add(f.id)
            roots.append((f, why))

    def vendor(f):
        return bool(RLV_VENDOR_RE.match(f.name))

    def walk(f):
        if (f.ptype in EXCLUDED_PTYPES and f.ptype != "rlv") or vendor(f):
            if vendor(f):
                stats["left alone: vendor-managed folder (~)"] += 1
            return
        kind = inv.kind.get(f.id)
        path = inv.path(f)
        parts = path.split(SEP)
        kids = [c for c in f.children if c.ptype == "-1"]
        if parts[0] in RLV_HAND_ZONES:
            if path == SEP.join([RLV_ROOT, RLV_UTILITIES]):
                for c in kids:
                    if not vendor(c):
                        add(c)
                return
            if len(parts) == 1 or rlv_container_named(f.name):
                for c in kids:
                    if vendor(c):
                        stats["left alone: vendor-managed folder (~)"] += 1
                    elif rlv_container_named(c.name):
                        walk(c)
                    else:
                        walk(c)
                if inv.items[f.id] and len(parts) > 1 and not kids:
                    add(f, "loose")
                return
            if rlv_is_unit(inv, f):
                add(f)
                return
            for c in kids:
                walk(c)
            return
        if kind in ("product", "part") and not (kids and rlv_container_named(f.name)):
            add(f)
            return
        if kind == "part":
            add(f)
            return
        if kids and inv.count(f) and not rlv_container_named(f.name) and (
                all(rlv_partish(c, f) for c in kids)
                or (len(kids) >= 2 and 2 * sum(1 for c in kids if rlv_partish(c, f)) >= len(kids))):
            add(f)
            return
        if inv.items[f.id] and not kids and inv.count(f):
            add(f, "loose" if rlv_container_named(f.name) else "")
            return
        for c in kids:
            walk(c)
        if kind == "product" and not kids:
            add(f)

    for f in sorted(inv.folders.values(), key=lambda f: inv.path(f)):
        path = inv.path(f)
        if any(path == p or path.startswith(p + SEP) for p in RLV_SCOPE):
            parent = inv.folders.get(f.parent)
            if parent is not None and parent is not inv.root and any(
                    inv.path(parent) == p or inv.path(parent).startswith(p + SEP) for p in RLV_SCOPE):
                continue                      # reached through its parent
            walk(f)
    return roots


def rlv_brand_names(inv, root, brands, mapped, evidence=None):
    """The brand names to strip from a product's name (the first is the one a name
    collision is told apart by). A brand read from the name alone must have a sign
    that it is one: a tag or separator after it, or another product with the same."""
    names = []
    parent = inv.folders.get(root.parent)
    explicit = bool(BRACKET_TAG_RE.match(root.name) or SYMBOL_TAG_RE.match(root.name)
                    or SEPARATORS_RE.search(root.name))
    cands = []                                              # (name, sure)
    for c in (brands.get(root.id, ""), own_brand_key(root.name)):
        count = (evidence or {}).get(squash(c), 0)
        sure = explicit or count >= 2
        rx = _brand_rx(c) if c else None
        after = re.match(r"^\W*(?:%s)[ ]+(\w+)" % rx, root.name, re.IGNORECASE) if rx else None
        if sure and count < 3 and after and rlv_keyword_types(after.group(1)):
            sure = False                    # "Squish Belt - Cornered": Squish is part of the product's name
        cands.append((c, sure))
    if parent is not None and parent is not inv.root and not is_generic_brand(parent.name):
        cands.append((clean_brand(parent.name), True))
    creator = inv.dominant_creator(root)
    if creator and inv.creator_names.get(creator):
        cands.append((clean_creator_name(inv.creator_names[creator]), True))
    m = BRACKET_TAG_RE.match(root.name)
    if m:
        cands.append((m.group(1).strip(), True))
    for c, sure in cands:
        c = mapped.get(squash(c), c) if c else c
        if (c and sure and c != "Unsorted" and not is_generic_brand(c) and c not in names
                and not rlv_keyword_types(c)):     # "Squish Belt" is a product, not a brand
            names.append(c)
    return names


def rlv_listing_type(inv, root, brand_names, cache, stats):
    """(area, type, weak, "category text") from the Marketplace listing that matches the
    folder's name, in the store of one of its brand names; None if there is no match."""
    for b in brand_names:
        entry = cache.get(squash(b)) if cache else None
        if not entry or not entry.get("listings"):
            continue
        own = rlv_short_name(root.name, brand_names)
        if not _informative(own) or squash(own) in {squash(x) for x in brand_names}:
            continue                      # only the brand's name: it would match any listing of the store
        m = match_listing(own, entry["listings"])
        if m and m[1]:
            # One word of a longer title is no match ("Eclipse" is not "Eclipse Thigh High Boots").
            theirs = listing_words(m[0]) - set().union(*(listing_words(x) for x in brand_names))
            if len(listing_words(own)) < 2 and not theirs <= listing_words(own):
                continue
            found = rlv_category(m[1])
            if found:
                return found + (" > ".join(m[1][-2:]),)
    return None


def rlv_lineage_name(inv, f, short, names):
    """A name that says nothing ("Black", "Left") borrows the names of the folders above it
    (up to three, nearest first) until it does: "Leather Hand Mitten Cuff Black"."""
    for a in inv.ancestors(f):
        if _informative(short) or a.parent == inv.root.id or RLV_VENDOR_RE.match(a.name)                 or (rlv_container_named(a.name) and a.depth <= 3
                    and len(a.name.strip(" .").split()) == 1):          # Area > Type: not part of a name
            break
        piece = rlv_short_name(a.name, names)
        if not piece or piece.casefold() in short.casefold():
            continue
        short = (piece + " " + short)[:RLV_NAME_MAX].rstrip()
    return short


def rlv_plan(inv, data, store_cache, stats=None):
    """Job 4: (plan rows, review rows). Plan rows are folder-plan rows
    (PLAN_FIELDS) moving each product folder to its #RLV home under a short name;
    review rows are one per product (n, kind plan|review, product_folder, from,
    proposed_to, short_name, why), sorted by proposed_to then name. Products already
    in place (canonical path, canonical name), unopened boxes, husks, vendor-managed
    folders and body parts are not in either."""
    stats = stats if stats is not None else Counter()
    mapped = _brand_map_lookup((data or {}).get("brands"))
    brands = product_brands(inv)
    evidence = Counter(squash(v) for v in brands.values())
    evidence.update(squash(own_brand_key(f.name)) for f in inv.folders.values() if inv.kind.get(f.id) == "product")
    existing = {inv.path(f) for f in inv.folders.values()}
    cands, review = [], []

    def hold(f, why, proposed="", short=""):
        stats["review: " + why.split(":")[0]] += 1
        review.append({"kind": "review", "product_folder": f.name, "from": inv.path(inv.folders.get(f.parent)) or "(root)",
                       "proposed_to": proposed, "short_name": short, "why": why, "folder": f})

    for f, flag in rlv_roots(inv, stats):
        path = inv.path(f)
        in_rlv = path.split(SEP)[0] == RLV_ROOT
        parent = inv.folders.get(f.parent)
        names = rlv_brand_names(inv, f, brands, mapped, evidence)
        short = rlv_lineage_name(inv, f, rlv_short_name(f.name, names), names)
        if flag == "loose":
            hold(f, "loose items in a type folder", short=short)
            continue
        types = inv.types(f)
        if set(types) <= LEFTOVER_TYPES:
            stats["left alone: husk (only landmarks, notecards, textures)"] += 1
            continue
        if not in_rlv:
            kind, why = classify_box(inv, f)
            if kind == "HUSK":
                stats["left alone: husk (only landmarks, notecards, textures)"] += 1
                continue
            if kind in ("BOXED", "SINGLE"):
                hold(f, "box: %s (%s); stays in Objects > To Unpack" % (kind, why), short=short)
                continue
        if re.search(r"\bdemo\b", f.name, re.IGNORECASE):
            hold(f, "demo: name says demo", short=short)
            continue
        objects = [n for d in subtree(inv, f) for ty, _, n in inv.items[d.id] if ty == "object"]
        hud_only = bool(objects) and not types.get("clothing", 0) and all(RLV_HUDISH_RE.search(n) for n in objects)
        if RLV_BODYPART_RE.match(f.name.strip(" .~")) or (
                set(types) <= {"bodypart"} and not path.startswith("Body Parts > Accessories")):
            stats["left alone: body part (out of scope)"] += 1
            continue
        context = bool(RLV_RESTRAINT_CONTEXT_RE.search(path))
        text = rlv_short_name(f.name, names, keep_version=True)
        full_text = rlv_short_name(f.name, [], keep_version=True)       # the brand may be the product's noun
        # 1. Utilities: an RLV tool by name.
        parts = path.split(SEP)
        direct_util = parts[:2] == [RLV_ROOT, RLV_UTILITIES] and len(parts) == 3
        if direct_util or (RLV_UTILITY_RE.search(full_text) and rlv_name_type(full_text, context) is None):
            area, kind, because = RLV_UTILITIES, "", "RLV tool by name" if not direct_util else "already under Utilities"
        else:
            if hud_only:
                hold(f, "HUD/applier only: no wearable, not an RLV tool by name", short=short)
                continue
            area = kind = because = None
            listing = rlv_listing_type(inv, f, names, store_cache, stats)
            name_hit = rlv_name_type(text, context) or rlv_name_type(full_text, context)
            if listing and listing[0] == "OUT":
                stats["left alone: body part (Marketplace category %s)" % listing[3]] += 1
                continue
            if name_hit and name_hit[0] == "MULTI":
                hold(f, "several types: " + name_hit[1], short=short)
                continue
            lst_area, lst_type, weak = (listing[0], listing[1], listing[2]) if listing else (None, None, False)
            if lst_type and not (weak and name_hit and name_hit[0] != "MULTI") and not (
                    lst_area != "Restraints" and name_hit and name_hit[0] == "Restraints"):
                area, kind, because = lst_area, lst_type, "MP category %s" % listing[3]
            elif name_hit and name_hit[0] != "MULTI":
                area, kind = name_hit
                because = "name keyword" + ("; MP category %s ignored" % listing[3] if listing else "")
            elif lst_area == "Restraints" or (listing and lst_type):
                area, kind, because = lst_area, lst_type or "Other", "MP category %s" % listing[3]
            if area is None:
                for a in inv.ancestors(f):
                    if a.parent == inv.root.id or is_brand_folder(inv, a) or squash(a.name) in {squash(n) for n in names}:
                        continue
                    hit = rlv_name_type(a.name.strip(" ."), context)
                    if hit and hit[0] != "MULTI":
                        area, kind, because = hit[0], hit[1], "folder '%s'" % a.name
                        break
            if area is None:
                hits = Counter()
                for d in subtree(inv, f):
                    for ty, _, n in inv.items[d.id]:
                        if ty in ("object", "clothing") and not RLV_HUDISH_RE.search(n):
                            h = rlv_name_type(n, context)
                            if h and h[0] != "MULTI":
                                hits[h] += 1
                if len(hits) == 1 and not in_rlv and inv.count(f) >= RLV_LOOSE_MIN and not f.children:
                    hold(f, "loose items: %d items typed only from their names, probably several products"
                         % inv.count(f), short=short)
                    continue
                if len(hits) == 1:
                    (area, kind), = hits
                    because = "item names"
                elif hits:
                    hold(f, "several types: " + ", ".join("%s > %s" % h for h in hits), short=short)
                    continue
            if area is None:
                hold(f, "no confident type", short=short)
                continue
            if kind == "Cuffs - Arms":
                if RLV_LEG_RE.search(full_text) and RLV_ARM_RE.search(full_text):
                    because += "; also legs"
                elif not RLV_ARM_RE.search(full_text) and not RLV_LEG_RE.search(full_text):
                    because += "; limb not named"
            because = "%s > %s: %s" % (area, kind, because)
        if area == RLV_UTILITIES:
            because = "Utilities: " + because
        cands.append({"folder": f, "area": area, "type": kind, "dest": rlv_dest(area, kind), "short": short,
                      "alts": [rlv_short_name(f.name, names, keep_version=True),
                               rlv_short_name(f.name, names, keep_body=True),
                               rlv_short_name(f.name, names, keep_version=True, keep_body=True)],
                      "brand": names[0] if names else "",
                      "because": because, "path": path, "names": names, "in_place": False})

    # Names under one destination must be unique (the checker refuses a clash). Products
    # already in place count as members, so a newcomer cannot take their name.
    for c in cands:
        parent = c["folder"].parent
        c["in_place"] = inv.path(inv.folders[parent]) == c["dest"] if parent in inv.folders else False
    moving = {c["folder"].id for c in cands}
    reserved = defaultdict(set)         # dest -> names of folders there that are not candidates
    for fo in inv.folders.values():
        if fo.id not in moving and fo.parent in inv.folders:
            p = inv.path(inv.folders[fo.parent])
            if rlv_canonical_dest(p):
                reserved[p].add(fo.name.casefold())
    groups = defaultdict(list)
    for c in cands:
        groups[(c["dest"], c["short"].casefold())].append(c)
    for (dest, _), members in sorted(groups.items(), key=lambda kv: kv[0]):
        clash = len(members) > 1 or members[0]["short"].casefold() in reserved[dest]
        if not clash:
            continue
        for n in range(3):          # the version, the body name, both: a different name each
            alts = [m["alts"][n].casefold() for m in members]
            if len(set(alts)) == len(members) and not any(a in reserved[dest] for a in alts):
                for m in members:
                    m["short"] = m["alts"][n]
                break
        else:
            alts = None
        if alts is not None and len(set(alts)) == len(members) and not any(a in reserved[dest] for a in alts):
            continue
        for m in members:
            tag = m["brand"]
            if tag and squash(tag) != squash(m["short"]):
                base = m["short"][:RLV_NAME_MAX - len(tag) - 3].rstrip()
                m["short"] = "%s (%s)" % (base, tag)
    final = defaultdict(list)
    for c in cands:
        final[(c["dest"], c["short"].casefold())].append(c)
    rows = []
    for c in cands:
        f = c["folder"]
        dups = final[(c["dest"], c["short"].casefold())]
        parent_path = inv.path(inv.folders.get(f.parent)) or "(root)"
        if len(dups) > 1 or c["short"].casefold() in reserved[c["dest"]]:
            if c["in_place"] and f.name == c["short"]:
                continue
            other = next((d["path"] for d in dups if d is not c), "")
            hold(f, "name collision: another product (%s) is called %s in %s" % (other, c["short"], c["dest"]),
                 proposed=c["dest"], short=c["short"])
            continue
        if c["in_place"] and f.name == c["short"]:
            stats["already in place"] += 1
            continue
        rename = c["short"] if c["short"] != f.name else ""
        row = {"product_folder": f.name, "items": inv.count(f), "from": parent_path, "to": c["dest"],
               "because": c["because"] + "; name '%s'" % f.name,
               "creates_folder": "" if c["dest"] in existing else "yes", "folder_id": str(f.id),
               "from_id": str(f.parent), "rename_to": rename, "allow_protected": "yes"}
        rows.append(row)
        stats["plan: %s" % (c["dest"][len(RLV_ROOT) + len(SEP):] if c["area"] != RLV_UTILITIES else "Utilities")] += 1
        review.append({"kind": "plan", "product_folder": f.name, "from": parent_path, "proposed_to": c["dest"],
                       "short_name": c["short"], "why": c["because"], "folder": f})
    review.sort(key=lambda r: (r["proposed_to"], r["short_name"].casefold(), r["product_folder"].casefold()))
    for n, r in enumerate(review, 1):
        r["n"] = n
    rows.sort(key=lambda r: (r["to"], (r["rename_to"] or r["product_folder"]).casefold()))
    return rows, review


REVIEW_FIELDS = ["n", "kind", "product_folder", "from", "proposed_to", "short_name", "why"]


def write_folder_plan(rows, path):
    """Write folder-plan rows (PLAN_FIELDS) as UTF-8 CSV with a BOM, like write_plan."""
    for r in rows:
        if set(r) != set(PLAN_FIELDS):
            raise ValueError("a plan row must have exactly the plan fields")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=PLAN_FIELDS)
        w.writeheader()
        w.writerows(rows)


def write_rlv_review(review, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(REVIEW_FIELDS)
        for r in review:
            w.writerow([str(r[k]).replace("\t", " ") for k in REVIEW_FIELDS])


def load_inventory(path):
    meta, categories, items = icr.read_cache(path)
    return Inventory(categories, items, getattr(meta, "creators", None))


def plan_command(argv, out=sys.stdout):
    """prune / docs / dedupe: write an action plan from a Hangar export (or cache)."""
    parser = argparse.ArgumentParser(
        prog="inventory_triage.py", description="Item-level action plans (drafts, nothing is applied).")
    sub = parser.add_subparsers(dest="job", required=True)
    for job, text in (("prune", "trash vendor landmarks and ad textures inside product folders"),
                      ("docs", "move notecards in product folders to Notecards > Docs > <Brand>")):
        p = sub.add_parser(job, help=text)
        p.add_argument("export", help="a hangar-export JSON file (or an inventory cache)")
        p.add_argument("out", metavar="plan.csv", help="the action plan to write")
        if job == "docs":
            p.add_argument("map", nargs="?", help="the triage map (its \"brands\" names brand keys)")
            p.add_argument("--dup-review", metavar="review.csv",
                           help="write the duplicate-notecard item_trash rows here and keep "
                           "only the item_move rows in the plan")
    p = sub.add_parser("rlv", help="file product folders under #RLV > Restraints / Clothes / Accessories / "
                       "Utilities > Type > Short name (a folder plan, for review)")
    p.add_argument("export", help="a hangar-export JSON file (or an inventory cache)")
    p.add_argument("out", metavar="plan.csv", help="the folder plan to write")
    p.add_argument("review", metavar="review.tsv", help="the numbered review list of every product")
    p.add_argument("map", nargs="?", help="the triage map (its \"brands\" names brand keys)")
    p.add_argument("--store-cache", default=None, metavar="JSON",
                   help="the Marketplace store cache (default: store_cache.json in the current folder, if present)")
    p = sub.add_parser("empties", help="trash folders that are empty recursively under the given roots")
    p.add_argument("export", help="a hangar-export JSON file (or an inventory cache)")
    p.add_argument("out", metavar="plan.csv", help="the action plan to write")
    p.add_argument("--under", action="append", metavar="PATH",
                   help="a root folder path such as \"#RLV\" (repeatable; default: #RLV)")
    p = sub.add_parser("dedupe", help="find same-name product folders; trash boxes and identical copies")
    p.add_argument("export", help="a hangar-export JSON file (or an inventory cache)")
    p.add_argument("review", metavar="review.tsv", help="the review list of every group")
    p.add_argument("out", metavar="plan.csv", help="the action plan (rule-decided rows only)")
    args = parser.parse_args(argv)
    try:
        inv = load_inventory(args.export)
    except (OSError, ValueError, EOFError, icr.llsd.LLSDParseError) as exc:
        sys.exit("could not read %s: %s" % (args.export, exc))
    stats = Counter()
    if args.job == "rlv":
        cache_path = args.store_cache or ("store_cache.json" if os.path.exists("store_cache.json") else "")
        rows, review = rlv_plan(inv, load_map(args.map), load_store_cache(cache_path), stats)
        write_folder_plan(rows, args.out)
        write_rlv_review(review, args.review)
        out.write("Plan written to %s: %d folder moves (%d items). Review list %s: %d products. "
                  "Store cache: %s. Nothing has been changed.\n" % (
                      args.out, len(rows), sum(r["items"] for r in rows), args.review, len(review),
                      cache_path or "none"))
        for why, n in sorted(stats.items()):
            out.write("  %5d  %s\n" % (n, why))
        return rows
    if args.job == "empties":
        rows = write_action_plan(empties_plan(inv, tuple(args.under or ["#RLV"])), args.out)
        out.write("Plan written to %s: %d folder_trash rows (topmost empty folders). Nothing has been changed.\n"
                  % (args.out, len(rows)))
        return rows
    if args.job == "dedupe":
        groups = dedupe_groups(inv)
        write_dedupe_review(inv, groups, args.review)
        rows = write_action_plan(dedupe_plan(inv, groups), args.out)
        out.write("Review list %s: %d groups (%s).\n" % (
            args.review, len(groups), ", ".join("%d %s" % (n, k) for k, n in
                                                 Counter(g["kind"] for g in groups).most_common())))
        out.write("Plan written to %s: %d folder_trash rows (%s). Nothing has been changed.\n" % (
            args.out, len(rows), ", ".join("%d %s" % (n, k) for k, n in
                                           Counter(r["because"].split(":")[0] for r in rows).most_common())))
        return rows
    if args.job == "docs":
        rows = docs_plan(inv, stats, load_map(args.map)["brands"])
        if args.dup_review:
            write_action_plan([r for r in rows if r["kind"] == "item_trash"], args.dup_review)
            rows = [r for r in rows if r["kind"] != "item_trash"]
        rows = write_action_plan(rows, args.out)
    else:
        rows = write_action_plan(prune_plan(inv, stats), args.out)
    by_rule = Counter((r["kind"], r["because"].split(" (")[0]) for r in rows)
    out.write("Plan written to %s: %d rows. Nothing has been changed.\n" % (args.out, len(rows)))
    for (kind, rule), n in by_rule.most_common():
        out.write("  %5d  %s: %s\n" % (n, kind, rule))
    for why, n in stats.most_common():
        out.write("  %5d  %s\n" % (n, why))
    return rows


# --------------------------------------------------------------------------
# Self-test

def _self_test():
    import tempfile
    import uuid

    ids = defaultdict(uuid.uuid4)
    me, alder, cedar, sable = (uuid.uuid4() for _ in range(4))
    null = uuid.UUID(int=0)

    def cat(key, parent, name, ptype="-1"):
        return {"cat_id": ids[key], "parent_id": ids[parent] if parent else null,
                "preferred_type": ptype, "name": name}

    def item(parent, name, creator, atype="object"):
        return {"item_id": uuid.uuid4(), "parent_id": ids[parent], "type": atype,
                "name": name, "permissions": {"creator_id": creator, "owner_id": me}}

    cats = [cat("root", None, "My Inventory", "root_inv"),
            cat("trash", "root", "Trash", "trash"),
            cat("inbox", "root", "Received Items", "inbox"),
            cat("clothing", "root", "Clothing", "clothing"),
            cat("clothes", "clothing", "Clothes"),
            cat("alder", "clothes", "ALDER"),
            cat("o1", "alder", "ALDER - Paige Tee"),
            cat("o2", "alder", "ALDER - Della Dress"),
            cat("birch", "clothes", "BIRCH"),
            cat("t1", "birch", "BIRCH - Sonya Set"),
            cat("t2", "birch", "BIRCH - Willow Set"),
            cat("objects", "root", "Objects", "object"),
            cat("building", "objects", "Building"),
            cat("cedar", "building", "Cedar"),
            cat("n1", "cedar", "Cedar. Farmhouse Kitchen"),
            cat("n2", "cedar", "Cedar. Colonial Bathroom"),
            cat("rlv", "root", "#RLV"),
            cat("rlvb", "rlv", ".Bramblewick"),
            cat("rb1", "rlvb", "[BW] Bramblewick - Bikini"),
            cat("rb2", "rlvb", "[BW] Bramblewick - Socks"),
            # unfiled
            cat("u1", "root", "ALDER - Hattie Sweater \\ Fullpack"),        # by name
            cat("u2", "inbox", "Mystery Tee (unpacked)"),                  # by creator
            cat("u3", "root", "Sable Rocky Shore Building Set"),            # new brand
            cat("u4", "root", "Sable Pebble Beach Building Set"),
            cat("u5", "inbox", "Tropical Beach from Studio Sable"),         # joins Sable by creator
            cat("u6", "root", "BIRCH - Luna Full Set"),
            cat("nts", "objects", "Need to Sort"),
            cat("u7", "nts", "Cedar. Guesthouse / add me"),               # staging, by name
            cat("u8", "root", "[BW] Bramblewick - Leotard"),               # only in #RLV: new
            # a brand already split by type across two folders
            cat("chairs", "building", "Chairs"),
            cat("afc", "chairs", "Pear Hollow"),
            cat("afc1", "afc", "Pear Hollow Wingback Chair"),
            cat("afc2", "afc", "Pear Hollow Club Chair"),
            cat("tables", "building", "Tables"),
            cat("aft", "tables", "Pear Hollow"),
            cat("aft1", "aft", "Pear Hollow Side Table"),
            cat("aft2", "aft", "Pear Hollow Dining Table"),
            cat("u9", "root", "Pear Hollow Reading Lamp"),
            # a product whose parts have parts is still one product, not a
            # brand folder holding "HUDs" and "Shoes"
            # a brand whose products hold generic parts (Hair, HUDs, Docs)
            cat("hairs", "root", "Body Parts", "bodypart"),
            cat("hair", "hairs", "Hair"),
            cat("mg", "hair", "Marigold"),
            cat("mgd", "mg", "Delilah"), cat("mgd1", "mgd", "Hair"), cat("mgd2", "mgd", "HUDs"),
            cat("mgd3", "mgd", "Docs"),
            cat("mge", "mg", "Eliza"), cat("mge1", "mge", "Hair"), cat("mge2", "mge", "HUDs"),
            cat("u12", "root", "Marigold - Ashley Hair"),
            cat("u15", "root", "Marigold - Eliza Hair"),          # clashes with a sibling
            # a brand folder whose products are named after a line ("Juniper ...")
            cat("co", "building", "Quill Harbour"),
            cat("co1", "co", "Juniper Pattycake Dress"), cat("co2", "co", "Juniper Naptime Dress"),
            cat("u13", "root", "{Juniper} Garter Fatpack"),
            cat("u14", "root", "Pierced Pasties 1.4 (box)"),
            cat("me", "clothes", "Pale Ember x LYRA"),
            cat("sa", "me", "Hidden Fan"),
            cat("sa1", "sa", "HUDs"),
            cat("sa2", "sa", "Shoes"),
            cat("sa3", "sa2", "Backup"),
            # "unpack" in a product's own name must not make it a staging folder
            cat("u10", "root", "R&D Rider Boots (wear to unpack)"),
            # landscaping: nothing like it is filed until the user decides Sable
            cat("u11", "root", "Grove Rock Garden"),
            # a big brand: split by type; its box from Received Items queues
            cat("h1", "root", "Hearth Farmhouse Kitchen (Add)"),
            cat("h2", "root", "Hearth Loft Kitchen (Add)"),
            cat("h3", "root", "Hearth Cottage Bedroom (Add)"),
            cat("h4", "root", "Hearth Nursery Bedroom (Add)"),
            cat("h5", "root", "Hearth Garden Bench"),
            cat("h6", "root", "Hearth Rustic Clock"),
            cat("h7", "inbox", "Hearth Pantry Shelf (Box)"),
            # a box filed earlier under the To Unpack destination: not a source
            cat("tu", "objects", "To Unpack"),
            cat("tuh", "tu", "Hearth"),
            cat("tuh1", "tuh", "Hearth Chest (Box)"),
            cat("h8", "root", "Hearth Attic Lamp (Box)"),
            cat("tue", "root", "To Unpack"),      # elsewhere: still staging
            # Marketplace: TRV's listings say kink accessories, not body parts
            cat("g1", "root", "TRV Pallas Pony Boots (box)"),
            cat("g2", "root", "TRV puffy straitjacket"),
            cat("g3", "inbox", "TRV [VESTA GASMASK FATPACK]"),
            cat("g4", "root", "TRV mats"),       # two words, no separator: still TRV
            cat("g5", "root", "TRV Pallas Pony Boots v2 (unpacked)"),
            cat("g6", "inbox", "TRV latex corset"),
            cat("materials", "root", "Materials", "material"),
            # a made-up category, learnt from Cedar's cached store
            cat("d1", "root", "Stonecraft Iron Maiden"),
            # a brand folder inside an area for boxed things: boxed MKR only
            cat("br", "objects", "Boxed Restraints"),
            cat("br1", "br", "MKR Classic Leather Bondage Belt"),
            cat("br2", "br", "MKR Posture Collar"),
            cat("k1", "root", "MKR Vermilion thigh cuffs"),
            cat("k1a", "k1", "Cuffs"),
            cat("k2", "root", "MKR X (box)"),
            ]
    items = []
    for k in ("o1", "o2", "u1", "u2"):
        items += [item(k, "ALDER dress %d" % i, alder, "clothing") for i in range(3)]
    for k in ("t1", "t2", "u6"):
        items += [item(k, "BIRCH top %d" % i, None or uuid.uuid4(), "clothing") for i in range(2)]
    for k in ("n1", "n2", "u7"):
        items += [item(k, "Cedar chair table lamp %d" % i, null) for i in range(4)]
    for k in ("u3", "u4", "u5"):
        items += [item(k, "Rock beach stone %d" % i, sable) for i in range(3)]
    for k in ("rb1", "rb2", "u8"):
        items += [item(k, "BW bikini %d" % i, null, "clothing") for i in range(2)]
    for k in ("afc1", "afc2", "aft1", "aft2", "u9"):
        items += [item(k, "Pear Hollow piece %d" % i, null) for i in range(2)]
    items += [item("u10", "R&D boots Legacy %d" % i, null, "clothing") for i in range(2)]
    items += [item(k, "Hidden Fan %s" % k, null) for k in ("sa1", "sa2", "sa3")]
    marigold, curio, juniper = (uuid.uuid4() for _ in range(3))
    # Delilah and Eliza come from different creators, as in real data, so
    # Marigold is a "bucket" (products that do not carry the brand), not a "brand".
    items += [item(k, "Marigold hair %s %d" % (k, i), uuid.uuid4() if k in ("mgd1", "mgd2", "mge1", "mge2") else marigold)
              for k in ("mgd1", "mgd2", "mge1", "mge2", "u12", "u15") for i in range(2)]
    items.append(item("mgd3", "Marigold docs", marigold, "notecard"))
    items += [item(k, "Juniper dress %d" % i, curio, "clothing") for k in ("co1", "co2") for i in range(2)]
    items += [item("u13", "Juniper garter %d" % i, juniper, "clothing") for i in range(2)]
    items.append(item("u14", "Pierced Pasties 1.4 (box)", null))
    items += [item("u11", "Rock beach stone boulder %d" % i, null) for i in range(2)]
    for k in ("h1", "h2", "h3", "h4", "h5", "h6"):
        items += [item(k, "Hearth piece %d" % i, null) for i in range(3)]
    items.append(item("h7", "Hearth Pantry Shelf (Box)", null))
    items.append(item("tuh1", "Hearth Chest (Box)", null))
    items.append(item("h8", "Hearth Attic Lamp (Box)", null))
    items += [item(k, "TRV thing %d" % i, null) for k in ("g2", "g3", "g5", "g6") for i in range(2)]
    items.append(item("g1", "TRV Pallas Pony Boots (box)", null))
    items += [item("g4", "TRV normal map %d" % i, null, "material") for i in range(2)]
    items += [item("d1", "Iron Maiden %d" % i, null) for i in range(2)]
    mkr = uuid.uuid4()
    items += [item(k, "MKR piece %d" % i, mkr) for k in ("k1", "k1a") for i in range(2)]
    items += [item(k, "MKR %s (box)" % k, mkr) for k in ("br1", "br2")]
    items.append(item("k2", "MKR X (box)", mkr))
    items += [item("nts", "stray thing %d" % i, uuid.uuid4()) for i in range(6)]
    items[-1]["permissions"]["creator_id"] = items[-2]["permissions"]["creator_id"]
    items[-3]["permissions"]["creator_id"] = items[-4]["permissions"]["creator_id"]
    items[-5]["permissions"]["creator_id"] = items[-6]["permissions"]["creator_id"]

    inv = Inventory(cats, items)
    tri = Triage(inv)
    by_name = {g.label: g for g in tri.groups}
    sable_weak_before = by_name.get("Sable") and by_name["Sable"].weak
    checks = []

    def check(label, got, want):
        checks.append((label, got, want))

    check("generic part subfolders make one product",
          [inv.kind[ids[k]] for k in ("mg", "mgd", "mgd1", "mge")], ["bucket", "product", "part", "product"])
    check("Marigold is a bucket, as in real data", inv.kind[ids["mg"]], "bucket")
    check("a creator match files into the brand, not into a product",
          by_name.get("Marigold") and by_name["Marigold"].match, ("Body Parts > Hair > Marigold", "brand name"))
    tri.apply_brand_names({"pierced": "Verne Industries"})
    by_name = {g.label: g for g in tri.groups}
    check("a corrected brand name renames a boxed group's destination",
          by_name.get("Verne Industries (boxed)") and by_name["Verne Industries (boxed)"].match,
          ("Objects > To Unpack > Verne Industries", "boxed"))
    check("a product-line name does not claim another creator's product",
          by_name.get("Juniper") is not None and by_name["Juniper"].match, None)
    mkr_open = by_name.get("MKR")
    check("a brand folder in a boxed area is a boxed home only",
          (inv.kind[ids["br"]], bool(tri.known.get("mkr")), len(tri.boxed_homes["mkr"])), ("brand", False, 1))
    check("unpacked MKR is not matched to Boxed Restraints",
          mkr_open is not None and mkr_open.match, None)
    check("unpacked MKR has no suggestion under a boxed area",
          mkr_open is not None and [d for d, _, _ in mkr_open.suggestions if Triage.boxed_path(d)], [])
    check("boxed MKR still goes to To Unpack",
          by_name.get("MKR (boxed)") and by_name["MKR (boxed)"].match,
          ("Objects > To Unpack > MKR", "boxed"))
    check("boxed-area words are whole words",
          [bool(BOXED_AREA_RE.search(n)) for n in
           ("Boxed Restraints", "Boxes", "To Unpack", "Unpack Later", "Packaged", "Toolbox", "Boxer", "Unpacked", "Hatbox")],
          [True, True, True, True, True, False, False, False, False])
    check("a product with nested parts is a product",
          [inv.kind[ids[k]] for k in ("sa", "sa1", "sa2", "sa3")],
          ["product", "part", "part", "part"])
    check("ALDER auto by name", by_name.get("ALDER") and by_name["ALDER"].match,
          ("Clothing > Clothes > ALDER", "brand name"))
    check("ALDER folder count", len(by_name["ALDER"].products), 2)
    check("BIRCH auto", by_name.get("BIRCH") and by_name["BIRCH"].match[0], "Clothing > Clothes > BIRCH")
    check("Cedar from Need to Sort", by_name.get("Cedar") and by_name["Cedar"].match[0],
          "Objects > Building > Cedar")
    sable_group = by_name.get("Sable")
    check("Sable grouped by name and creator", sable_group and len(sable_group.products), 3)
    check("Sable flagged as a weak match", sable_weak_before, True)
    check("#RLV brand not a destination", by_name.get("BW") is not None and by_name["BW"].match, None)
    check("RLV folders not sources", any(f.name.endswith("Bikini") for g in tri.groups
                                         for f, _, _ in g.products), False)
    check("mixed staging folder listed", [p for p, _ in tri.manual], ["Objects > Need to Sort"])
    apple = by_name.get("Pear Hollow")
    check("split brand is a decision, not automatic", apple is not None and apple.match, None)
    check("split brand offers brand-first first", apple and [s[0] for s in apple.suggestions],
          ["Objects > Building > Pear Hollow", "Objects > Building > Chairs > Pear Hollow",
           "Objects > Building > Tables > Pear Hollow"])
    hearth = {g.label: g for g in tri.groups if g.brand == "Hearth"}
    check("big brand split by type, odd ones together",
          sorted((l, len(g.products)) for l, g in hearth.items()),
          [("Hearth", 2), ("Hearth (boxed)", 2), ("Hearth / Bedroom", 2),
           ("Hearth / Kitchen & Dining", 2)])
    check("Received Items box queued", hearth["Hearth (boxed)"].match[0], "Objects > To Unpack > Hearth")
    srcs = {f.name for f in tri._sources()}
    check("a box filed under To Unpack is not a source",
          ("Hearth Chest (Box)" in srcs, "Hearth" in srcs, "To Unpack" in srcs, "Hearth Attic Lamp (Box)" in srcs),
          (False, False, False, True))
    check("a filed box produces no plan row",
          [f.name for g in tri.groups for f, _, _ in g.products if f.id == ids["tuh1"]], [])
    check("the new box joins the filed brand folder",
          (hearth["Hearth (boxed)"].match, sorted(f.name for f, _, _ in hearth["Hearth (boxed)"].products)),
          (("Objects > To Unpack > Hearth", "boxed"), ["Hearth Attic Lamp (Box)", "Hearth Pantry Shelf (Box)"]))
    check("the To Unpack brand folder is not an undecided product",
          [g.label for g in tri.groups if not g.match and any(
              f.id in (ids["tuh"], ids["tu"]) for f, _, _ in g.products)], [])
    check("a To Unpack folder elsewhere stays staging",
          (inv.staging(inv.folders[ids["tue"]]), inv.staging(inv.folders[ids["tuh1"]]),
           inv.staging(inv.folders[ids["tu"]])), (True, False, False))
    check("type decision suggests Brand > Type",
          hearth["Hearth / Bedroom"].suggestions[0][0].endswith("Hearth > Bedroom"), True)
    # Marketplace categories feed the suggestions
    store_cache = {
        "trv": {"store": "171122", "pages": 1, "done": True, "listings": [
            ("TRV Pallas Pony Boots", ["Apparel", "Women's", "Women's Footwear", "Women's Boots"]),
            ("TRV puffy straitjacket", ["Avatar Accessories", "BDSM Accessories"]),
            ("TRV [VESTA GASMASK] fatpack", ["Avatar Accessories", "BDSM Accessories"]),
            ("TRV something else", ["Avatar Accessories", "BDSM Accessories"]),
            ("TRV gloves", ["Avatar Accessories", "Hats, Gloves, and Scarves"]),
            ("TRV latex corset", ["Apparel", "Women's", "Women's Tops", "Women's Corsets"])]},
        "cedar": {"store": "1", "pages": 1, "done": True, "listings": [
            ("Cedar. Rack %d" % i, ["Fantasy", "Dungeon"]) for i in range(4)] + [
            ("Cedar. Cuffs %d" % i, ["Avatar Accessories", "BDSM Accessories"]) for i in range(3)]},
        "stonecraft": {"store": "2", "pages": 1, "done": True, "listings": [
            ("Stonecraft Iron Maiden", ["Fantasy", "Dungeon"])]},
    }
    check("listing matched by name",
          match_listing("TRV Pallas Pony Boots (box)", store_cache["trv"]["listings"]),
          ("TRV Pallas Pony Boots", ["Apparel", "Women's", "Women's Footwear", "Women's Boots"]))
    check("no listing match on one shared word",
          match_listing("TRV hat", store_cache["trv"]["listings"]), None)
    check("listing matched despite body names and sizes in its title",
          match_listing('**Harbour@Design** - "KESTREL"', [
              ('**Harbour@Design** - "KESTREL" - Maitreya Lara, LaraX, Legacy, Kupra, Reborn, GenX',
               ["Apparel", "Women's", "Women's Footwear"]),
              ('**Harbour@Design** - "ROWENA" - Maitreya, Legacy', ["Apparel"])])[1],
          ["Apparel", "Women's", "Women's Footwear"])
    check("seed area by longest prefix",
          area_for_category(["Avatar Accessories", "BDSM Accessories", "BDSM Collars"]),
          "Clothing > Accessories > Kink")
    check("seed area prefers an existing folder",
          area_for_category(["Avatar Accessories", "BDSM Accessories"],
                            exists=lambda p: p == "Clothing > Kink"),
          "Clothing > Kink")
    tri.use_store_cache(store_cache)
    by_name = {g.label: g for g in tri.groups}
    check("category learnt from a filed brand's store",
          tri.category_areas.get("Fantasy > Dungeon"), "Objects > Building")
    check("one brand cannot override a seeded category",
          tri.category_areas.get("Avatar Accessories > BDSM Accessories"), None)
    trv = by_name.get("TRV / Kink")
    check("brand split by Marketplace area",
          sorted((g.label, len(g.products)) for g in tri.groups if g.brand == "TRV"),
          [("TRV (boxed)", 1), ("TRV / Clothes", 2), ("TRV / Kink", 2), ("TRV / Textures", 1)])
    check("two-word name joins its brand", by_name.get("TRV") is None, True)
    check("area split suggests the area first",
          by_name.get("TRV / Clothes") and by_name["TRV / Clothes"].suggestions[0][0],
          "Clothing > Clothes > TRV")
    tex = by_name.get("TRV / Textures")
    check("texture-only product split off to Materials",
          tex and [f.name for f, _, _ in tex.products] + [s[0] for s in tex.suggestions],
          ["TRV mats", "Materials > TRV"])
    check("Marketplace listings put TRV in Kink", trv and trv.suggestions[0][0],
          "Clothing > Accessories > Kink > TRV")
    check("Marketplace basis shown", trv and trv.suggestions[0][2],
          "Marketplace listings (2 of 2 matched): Avatar Accessories > BDSM Accessories 100%")
    check("Marketplace suggestion is not weak", trv and trv.weak, False)
    check("TRV box still queued",
          by_name.get("TRV (boxed)") and by_name["TRV (boxed)"].match[0],
          "Objects > To Unpack > TRV")
    dw = by_name.get("Stonecraft")
    check("learnt category used for a new brand", dw and dw.suggestions[0][0],
          "Objects > Building > Stonecraft")
    check("marketplace summary counts matches", marketplace_summary(store_cache, trv),
          "Avatar Accessories > BDSM Accessories 50%, Apparel > Women's 33%, "
          "Avatar Accessories > Hats, Gloves, and Scarves 17%; 2 of 2 products matched a listing")
    sp = by_name.get("R&D (boxed)")
    check("an unpacker goes to the To Unpack queue", sp and sp.match,
          ("Objects > To Unpack > R&D", "boxed"))

    import io
    with tempfile.TemporaryDirectory() as tmp:
        map_path = os.path.join(tmp, "map.json")
        screen = io.StringIO()

        def ask(_):
            # Accept every suggestion, except type paths for BW and Sable.
            shown = screen.getvalue().rsplit("\n[", 1)[-1].split("]", 1)[1]
            if shown.startswith(" BW "):
                return "Clothing > Clothes > Bramblewick"
            if shown.startswith(" Sable "):
                return "Objects > Landscaping > Sable"
            return ""

        data = load_map(map_path)
        interactive(tri, map_path, data, ask=ask, out=screen)
        data = load_map(map_path)
        dests = {d["brand"]: d["dest"] for d in data["decisions"].values()}
        check("typed path saved", dests.get("BW"), "Clothing > Clothes > Bramblewick")
        check("new area typed for Sable", dests.get("Sable"), "Objects > Landscaping > Sable")
        check("a brand like Sable is then suggested there", dests.get("Grove"),
              "Objects > Landscaping > Grove")
        check("split brand decision saved", dests.get("Pear Hollow"), "Objects > Building > Pear Hollow")
        interactive(tri, map_path, data, ask=lambda _: "q", out=io.StringIO())
        check("resume keeps decisions", len(load_map(map_path)["decisions"]), 13)
        stores = os.path.join(tmp, "stores.csv")
        rows = write_stores(tri, load_map(map_path), stores, out=io.StringIO())
        check("store list has unfiled brands, not filed ones",
              ("Sable" in {r["brand"] for r in rows}, "ALDER" in {r["brand"] for r in rows}),
              (True, False))
        with open(stores, newline="", encoding="utf-8-sig") as fh:
            edited = list(csv.DictReader(fh))
        for r in edited:
            if r["brand"] == "Sable":
                r["store_url"] = "https://marketplace.secondlife.com/stores/1"
        with open(stores, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=STORE_FIELDS)
            w.writeheader()
            w.writerows(edited)
        rows = write_stores(tri, load_map(map_path), stores, out=io.StringIO())
        check("typed store URL survives a refresh",
              [r["store_url"] for r in rows if r["brand"] == "Sable"],
              ["https://marketplace.secondlife.com/stores/1"])
        planmap = load_map(map_path)
        planmap["decisions"]["juniper"] = {"dest": "#RLV > Garters", "rename": "Garter"}
        rows = write_plan(tri, planmap, os.path.join(tmp, "plan.csv"), out=io.StringIO())
        check("plan rows", len(rows), 32)
        check("a decision into #RLV is flagged as deliberate and carries its rename",
              [(r["to"], r["rename_to"], r["allow_protected"]) for r in rows
               if r["product_folder"].startswith("{Juniper}")], [("#RLV > Garters", "Garter", "yes")])
        check("no other row is flagged",
              any(r["allow_protected"] for r in rows
                  if not r["product_folder"].startswith("{Juniper}")), False)
        check("the only automatic rename is Marigold's Ashley",
              {r["product_folder"]: r["rename_to"] for r in rows
               if r["rename_to"] and not r["product_folder"].startswith("{Juniper}")},
              {"Marigold - Ashley Hair": "Ashley"})
        check("the reason says so",
              [r["because"] for r in rows if r["rename_to"] == "Ashley"],
              ["existing brand folder (brand name); renamed to match Marigold's naming"])
        check("a name clash with a sibling is left alone",
              [r["rename_to"] for r in rows if r["product_folder"] == "Marigold - Eliza Hair"], [""])
        check("siblings that carry the brand (ALDER - Paige Tee) mean no rename",
              [r["rename_to"] for r in rows if r["to"] == "Clothing > Clothes > ALDER"], ["", ""])
        keep = load_map(map_path)
        keep["decisions"]["marigold"] = {"brand": "Marigold", "dest": "Body Parts > Hair > Marigold",
                                       "keep_names": True}
        check("keep_names opts the brand out",
              [r["rename_to"] for r in write_plan(tri, keep, os.path.join(tmp, "k.csv"), out=io.StringIO())
               if r["to"] == "Body Parts > Hair > Marigold"], ["", ""])
        marigold = {g.label: g for g in tri.groups}["Marigold"]
        marigold.boxed = True
        check("boxed groups are never renamed",
              [r["rename_to"] for r in write_plan(tri, planmap, os.path.join(tmp, "b.csv"), out=io.StringIO())
               if r["product_folder"].startswith("Marigold - ")], ["", ""])
        marigold.boxed = False
        marigold.match = None
        wins = load_map(map_path)
        both = marigold.products
        marigold.products = [p for p in both if p[0].name == "Marigold - Ashley Hair"]
        wins["decisions"]["marigold"] = {"dest": "Body Parts > Hair > Marigold", "rename": "Ash"}
        check("a decision's rename wins over the proposed one",
              [(r["rename_to"], r["because"]) for r in write_plan(tri, wins, os.path.join(tmp, "w.csv"), out=io.StringIO())
               if r["product_folder"] == "Marigold - Ashley Hair"], [("Ash", "your decision")])
        wins["decisions"]["marigold"] = {"dest": "Body Parts > Hair > Marigold"}
        marigold.products = both
        check("without one it is proposed for a decision's destination too",
              [r["rename_to"] for r in write_plan(tri, wins, os.path.join(tmp, "w2.csv"), out=io.StringIO())
               if r["product_folder"] == "Marigold - Ashley Hair"], ["Ashley"])
        check("a category folder of several brands is not a brand convention",
              proposed_renames(tri, {g.label: g for g in tri.groups}["ALDER"], "Clothing > Clothes",
                               {"Clothing > Clothes": [inv.folders[ids["clothes"]]]}), {})
        check("name stripping: tag, type word, version",
              [propose_name(n, {"marigold", "bramblewick"}, ["Delilah", "Eliza"]) for n in
               ("[BW] Bramblewick - Luna Set v1.2", "-MENDED- Rose Outfit (v0.2)",
                "Marigold: Ivy Fatpack 1.4", "Marigold - Hair")],
              ["Luna", "Rose", "Ivy", ""])
        check("plan rows carry folder and parent ids",
              all(r["folder_id"] and r["from_id"] and r["folder_id"] != r["from_id"] for r in rows), True)
        check("plan never moves a source into itself",
              all(r["from"] != r["to"] for r in rows), True)

    page = ("<script>var product_dl_data_1 = { 'name': 'Pear Hollow Ephemera Collection', "
            "'id': '1', 'brand': 'Pear Hollow', 'category': "
            "'[&quot;Home and Garden&quot;, &quot;Decor&quot;]' };</script>")
    check("store page parsed", parse_store_page(page),
          [("Pear Hollow Ephemera Collection", ["Home and Garden", "Decor"])])
    with tempfile.TemporaryDirectory() as tmp:
        stores = os.path.join(tmp, "s.csv")
        with open(stores, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=STORE_FIELDS)
            w.writeheader()
            w.writerow({"brand": "Pear Hollow", "key": "applefall",
                        "store_url": "https://marketplace.secondlife.com/stores/91528"})
        fetched, naps = [], []
        cache_path = os.path.join(tmp, "c.json")
        fetch_stores(stores, cache_path, out=io.StringIO(),
                     fetch=lambda u: fetched.append(u) or page, sleep=naps.append)
        fetch_stores(stores, cache_path, out=io.StringIO(),
                     fetch=lambda u: fetched.append(u) or page, sleep=naps.append)
        check("store fetched once, then cached", fetched,
              ["https://marketplace.secondlife.com/stores/91528?search%5Bper_page%5D=96&page=1"])
        # Pacing: two stores capped at 20 pages each -> 40 requests, 39 gaps,
        # one rest (before request 21), random gaps otherwise.
        big = page * 48
        stores2 = os.path.join(tmp, "s2.csv")
        with open(stores2, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=STORE_FIELDS)
            w.writeheader()
            for key, store in (("a", "1"), ("b", "2")):
                w.writerow({"brand": key, "key": key,
                            "store_url": "https://marketplace.secondlife.com/stores/" + store})
        cache2 = os.path.join(tmp, "c2.json")
        naps = []
        fetch_stores(stores2, cache2, out=io.StringIO(), fetch=lambda u: big,
                     sleep=naps.append, rand=lambda lo, hi: (lo, hi))
        check("stores capped at 20 pages", [e["pages"] for e in load_store_cache(cache2).values()],
              [STORE_MAX_PAGES, STORE_MAX_PAGES])
        check("rest every 20 requests, random gap otherwise",
              (len(naps), naps.count(CRAWL_REST), naps.count(CRAWL_GAP)), (39, 1, 38))
        # A block page (no listings) stops the run and caches nothing.
        cache3 = os.path.join(tmp, "c3.json")
        try:
            fetch_stores(stores, cache3, out=io.StringIO(),
                         fetch=lambda u: "<html>Access Denied</html>", sleep=naps.append)
            blocked = False
        except MarketplaceBlocked:
            blocked = True
        check("block page stops the run", blocked, True)
        check("blocked store not cached", os.path.exists(cache3), False)
        # A real store page with no listings (adult-only, not signed in) is
        # recorded and the run moves on.
        cache4 = os.path.join(tmp, "c4.json")
        empty = "<html><title>Second Life Marketplace - X by Y</title>0 matching items</html>"
        fetch_stores(stores, cache4, out=io.StringIO(), fetch=lambda u: empty, sleep=naps.append)
        check("empty store page cached as no public listings",
              {k: (e["done"], e["listings"], e.get("public")) for k, e in load_store_cache(cache4).items()},
              {"applefall": (True, [], False)})
        # --private-only refetches that store with the cookie and nothing else.
        fetched = []
        fetch_stores(stores, cache4, out=io.StringIO(), fetch=lambda u: fetched.append(u) or page,
                     sleep=naps.append, cookie="x=y", private_only=True)
        fetch_stores(stores, cache4, out=io.StringIO(), fetch=lambda u: fetched.append(u) or page,
                     sleep=naps.append, cookie="x=y", private_only=True)
        e = load_store_cache(cache4)["applefall"]
        check("private-only refetches the private store once",
              (len(fetched), e.get("public"), len(e["listings"])), (1, True, 1))
        # Empty even signed in: marked empty, not retried.
        cache5 = os.path.join(tmp, "c5.json")
        fetch_stores(stores, cache5, out=io.StringIO(), fetch=lambda u: empty, sleep=naps.append)
        fetched = []
        for _ in range(2):
            fetch_stores(stores, cache5, out=io.StringIO(), fetch=lambda u: fetched.append(u) or empty,
                         sleep=naps.append, cookie="x=y", private_only=True)
        e = load_store_cache(cache5)["applefall"]
        check("empty when signed in is not retried",
              (len(fetched), e.get("public"), e.get("empty")), (1, True, True))
        g = Group("applefall")
        check("marketplace summary", marketplace_summary(load_store_cache(cache_path), g),
              "Home and Garden > Decor 100%")

    # A live export (hangar-export JSON) loads like a cache, with creator names.
    doc, eids = icr.synthetic_export()
    with tempfile.TemporaryDirectory() as tmp:
        epath = os.path.join(tmp, "hangar-export-test.json")
        with open(epath, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        meta, ecats, eitems = icr.read_cache(epath)
    einv = Inventory(ecats, eitems, getattr(meta, "creators", None))
    etri = Triage(einv)
    check("export: creator names kept on the inventory", einv.creator_names,
          {eids["brand"]: "Brand A (Brandy)"})
    check("export: cache without names has none", Inventory(ecats, eitems).creator_names, {})
    check("export: folders and items reach the inventory",
          (len(einv.folders), sum(len(v) for v in einv.items.values())), (7, 5))
    check("export: Triage runs on it",
          isinstance(etri.groups, list) and einv.root.name == "My Inventory", True)

    # Contents decide whether a folder is a box (classify_box), not its name.
    bcats = [cat("b_root", None, "My Inventory", "root_inv"),
             cat("b_inbox", "b_root", "Received Items", "inbox"),
             cat("b_obj", "b_root", "Objects", "object"),
             cat("b_tu", "b_obj", "To Unpack"),
             cat("b_zeta", "b_tu", "Zeta"),
             cat("b_z1", "b_zeta", "Zeta Crate (box)"),
             cat("b_z2", "b_zeta", "Zeta Vase (box)"),
             cat("b_z3", "b_zeta", "Zeta Opened Lamp"),          # opened, filed in the queue
             cat("b_area", "b_obj", "Boxed Things"),             # named as an area for boxes...
             cat("b_yara", "b_area", "Yara"),
             cat("b_y1", "b_yara", "Yara Hat (boxed)"),          # ...but these are opened
             cat("b_y2", "b_yara", "Yara Boots (boxed)"),
             cat("b_stash", "b_obj", "Stash"),                   # plain name, holds boxes
             cat("b_wren", "b_stash", "Wren"),
             cat("b_w1", "b_wren", "Wren Chair (box)"),
             cat("b_w2", "b_wren", "Wren Table (box)"),
             cat("b_a", "b_root", "Lumina Dress (boxed)"),
             cat("b_b", "b_root", "Lone Lamp"),
             cat("b_c", "b_root", "Vendor Info"),
             cat("b_d", "b_root", "Lila Opened (box)"),
             cat("b_e", "b_root", "Torturer (boxed)"),
             cat("b_f", "b_inbox", "Pair (boxed)"),
             cat("b_g", "b_root", "Set Bundle (boxed)"),
             cat("b_gd", "b_g", "Docs")]
    bme = uuid.uuid4()

    def bitem(parent, name, atype="object"):
        return item(parent, name, bme, atype)
    bitems = [bitem("b_z1", "Zeta Crate (box)"), bitem("b_z2", "Zeta Vase (box)")]
    bitems += [bitem("b_z3", "Zeta lamp %s" % c) for c in "abc"]
    bitems += [bitem(k, "Yara piece %d" % i, "clothing") for k in ("b_y1", "b_y2") for i in range(3)]
    bitems += [bitem("b_y1", "Yara hat"), bitem("b_y2", "Yara boots")]
    bitems += [bitem("b_w1", "Wren Chair (box)"), bitem("b_w2", "Wren Table (box)")]
    bitems += [bitem("b_a", "Lumina dress %d" % i, "clothing") for i in range(3)] + [bitem("b_a", "Lumina HUD")]
    bitems += [bitem("b_b", "Lone Lamp rezzer")]
    bitems += [bitem("b_c", "Vendor", "landmark"), bitem("b_c", "READ ME", "notecard")]
    bitems += [bitem("b_d", "Lila Opened")]
    bitems += [bitem("b_e", "Torturer 1.0 (chest)")]
    bitems += [bitem("b_f", "Pair one"), bitem("b_f", "Pair two")]
    bitems += [bitem("b_g", "Set Bundle (box)"), bitem("b_gd", "Set docs", "notecard")]
    binv = Inventory(bcats, bitems)
    btri = Triage(binv)

    def bf(key):
        return binv.folders[ids[key]]
    check("box classifier: opened wearables under a boxed name",
          (classify_box(binv, bf("b_a"))[0], is_boxed(binv, bf("b_a"))), ("UNPACKED", False))
    check("box classifier: one box-named object is a box",
          (classify_box(binv, bf("b_z1"))[0], is_boxed(binv, bf("b_z1"))), ("BOXED", True))
    check("box classifier: one plain object is SINGLE, not boxed",
          (classify_box(binv, bf("b_b"))[0], is_boxed(binv, bf("b_b"))), ("SINGLE", False))
    check("box classifier: landmark and notecard only is a husk",
          classify_box(binv, bf("b_c"))[0], "HUSK")
    check("box classifier: an opened box whose one object is the product",
          (classify_box(binv, bf("b_d"))[0], is_boxed(binv, bf("b_d"))), ("UNPACKED", False))
    check("box classifier: a worn attachment is never a box",
          classify_box(binv, bf("b_e"))[0], "UNPACKED")
    check("box classifier: two plain objects in a box-named delivery are ambiguous",
          classify_box(binv, bf("b_f"))[0], "AMBIG")
    check("box classifier: a subfolder means opened, with the reason",
          classify_box(binv, bf("b_g")), ("UNPACKED", "has subfolders"))
    check("a filed box stays a destination, an opened product in the queue is a source",
          [(binv.is_boxed_destination(bf(k)), binv.staging(bf(k))) for k in ("b_z1", "b_z3", "b_tu", "b_zeta")],
          [(True, False), (False, True), (True, False), (True, False)])
    check("the queue yields the opened product only",
          sorted(f.name for f in btri._sources() if binv.is_boxed_destination(f) is False
                 and binv.path(f).startswith(TO_UNPACK)), ["Zeta Opened Lamp"])
    check("a boxed-named area full of opened parts is an unpacked home",
          (bool(btri.known.get("yara")), len(btri.boxed_homes["yara"])), (True, 0))
    check("a plainly named folder of boxes is a boxed home",
          (bool(btri.known.get("wren")), len(btri.boxed_homes["wren"])), (False, 1))
    check("the queue's own brand folders stay boxed homes",
          len(btri.boxed_homes["zeta"]) > 0, True)

    # Action plans: the CSV format
    arows = [action_row("item_move", "Read me ♥", "notecard", "Clothing > Clothes > ALDER > Dress",
                        "Notecards > Docs > ALDER", "docs", "i1", "f1"),
             action_row("item_trash", "Store", "landmark", "Clothing > Clothes > ALDER > Dress", "",
                        "landmark in a product folder", "i2", "f1"),
             action_row("folder_trash", "Dress (box)", "category", "Clothing > Clothes > ALDER", "",
                        "box+opened", "f9", "f0")]
    with tempfile.TemporaryDirectory() as tmp:
        apath = os.path.join(tmp, "actions.csv")
        write_action_plan(arows, apath)
        with open(apath, "rb") as fh:
            raw = fh.read()
        with open(apath, newline="", encoding="utf-8-sig") as fh:
            back = list(csv.DictReader(fh))
    check("action plan header is exactly the format",
          raw.decode("utf-8-sig").splitlines()[0], "kind,item_name,asset_type,from,to,because,item_id,from_id")
    check("action plan is utf-8 with a BOM, like write_plan", raw[:3], b"\xef\xbb\xbf")
    check("action rows survive the round trip, names included", [r["item_name"] for r in back],
          ["Read me ♥", "Store", "Dress (box)"])
    check("trash kinds have no destination, a move has one", [r["to"] for r in back],
          ["Notecards > Docs > ALDER", "", ""])
    check("asset types use the export's spelling, folders say Folder",
          [r["asset_type"] for r in back], ["Notecard", "Landmark", "Folder"])
    check("a folder_trash carries the folder's id and its parent's",
          (back[2]["item_id"], back[2]["from_id"]), ("f9", "f0"))

    def refused(row):
        try:
            write_action_plan([row], os.devnull)
        except ValueError:
            return True
        return False
    check("a trash with a destination, a move without one, an unknown kind are refused",
          [refused(dict(arows[1], to="Trash")), refused(dict(arows[0], to="")), refused(dict(arows[0], kind="item_copy"))],
          [True, True, True])
    check("ACTION_FIELDS", ACTION_FIELDS,
          ["kind", "item_name", "asset_type", "from", "to", "because", "item_id", "from_id"])

    # Prune: landmarks and ad textures inside product folders
    pcats = [cat("p_root", None, "My Inventory", "root_inv"),
             cat("p_trash", "p_root", "Trash", "trash"),
             cat("p_lm", "p_root", "Landmarks", "landmark"),
             cat("p_clo", "p_root", "Clothing", "clothing"),
             cat("p_zora", "p_clo", "Zora"),
             cat("p_z1", "p_zora", "Zora Dress"),
             cat("p_z1d", "p_z1", "Docs"),                      # a part of the product
             cat("p_z2", "p_zora", "Zora Top"),
             cat("p_pax", "p_clo", "Pax"),
             cat("p_x1", "p_pax", "Pax Lace Tattoo applier"),   # a kit word in the name
             cat("p_x2", "p_pax", "Pax Bodysuit"),
             cat("p_mara", "p_clo", "Mara"),
             cat("p_m1", "p_mara", "Mara Chair"),               # three textures: a kit
             cat("p_m2", "p_mara", "Mara Poster Wall"),         # weak sign, shares "poster"
             cat("p_m3", "p_mara", "Mara Lamp"),                # a Textures subfolder
             cat("p_m3t", "p_m3", "Textures"),
             cat("p_m4", "p_mara", "Mara Rug"),                 # named like the store
             cat("p_m5", "p_mara", "Mara Vase"),                # no sign it is an ad
             cat("p_m6", "p_mara", "Mara Bowl"),                # a material: PBR product
             cat("p_places", "p_root", "Favourite places"),     # only landmarks
             cat("p_rlv", "p_root", "#RLV"),
             cat("p_rb", "p_rlv", "Cuffs"),
             cat("p_r1", "p_rb", "Cuffs - Steel"),
             cat("p_r2", "p_rb", "Cuffs - Iron"),
             cat("p_wi", "p_root", "Wardrobe Import"),
             cat("p_w1", "p_wi", "Imp Dress"),
             cat("p_w2", "p_wi", "Imp Top"),
             cat("p_old", "p_trash", "Old Dress")]
    pme = uuid.uuid4()

    def pitem(parent, name, atype="object"):
        return item(parent, name, pme, atype)
    pitems = []
    for k in ("p_z1", "p_z2", "p_x1", "p_x2", "p_m1", "p_m2", "p_m3", "p_m4", "p_m5", "p_m6", "p_r1", "p_r2",
              "p_w1", "p_w2", "p_old"):
        pitems.append(pitem(k, "%s part" % k))
    pitems += [pitem("p_z1", "Zora Store", "landmark"), pitem("p_z1", "Zora Dress AD", "texture"),
               pitem("p_z1", "Read me", "notecard"), pitem("p_z1d", "Zora landmark 2", "landmark"),
               pitem("p_z1d", "Zora manual", "notecard"),
               pitem("p_z2", "Zora Top Silk", "texture"),                  # no sign it is an ad
               pitem("p_x1", "Pax AD", "texture"),
               pitem("p_m1", "Mara Chair ad 1", "texture"), pitem("p_m1", "Mara Chair ad 2", "texture"),
               pitem("p_m1", "Mara Chair ad 3", "texture"),
               pitem("p_m2", "Poster", "texture"),
               pitem("p_m3t", "Lamp shade ad", "texture"),
               pitem("p_m4", "Salt & Pepper, The Birdcage", "landmark"), pitem("p_m4", "SALT & PEPPER", "texture"),
               pitem("p_m5", "Glass", "texture"),
               pitem("p_m6", "Mara Bowl PBR", "material"), pitem("p_m6", "Bowl ad", "texture"),
               pitem("p_places", "Cafe", "landmark"), pitem("p_places", "Beach", "landmark"),
               pitem("p_lm", "Loose landmark", "landmark"),
               pitem("p_r1", "Steel store", "landmark"), pitem("p_w1", "Imp store", "landmark"),
               pitem("p_old", "Old store", "landmark")]
    pinv = Inventory(pcats, pitems)
    pstats = Counter()
    prows = prune_plan(pinv, pstats)
    pby = {r["item_name"]: r for r in prows}
    check("prune: every landmark in a product folder or its part goes",
          sorted(n for n, r in pby.items() if r["asset_type"] == "Landmark"),
          ["Salt & Pepper, The Birdcage", "Steel store", "Zora Store", "Zora landmark 2"])
    check("prune: notecards, scripts and objects stay",
          [r["asset_type"] for r in prows if r["asset_type"] not in ("Landmark", "Texture")], [])
    check("prune: rows are item_trash with no destination and the folder ids",
          {(r["kind"], r["to"]) for r in prows}, {("item_trash", "")})
    check("prune: a row names the item's folder and ids",
          (pby["Zora Store"]["from"], pby["Zora Store"]["item_id"] != "", pby["Zora Store"]["from_id"]
           == str(ids["p_z1"])), ("Clothing > Zora > Zora Dress", True, True))
    check("prune: a landmark in the Landmarks folder, a places folder, #RLV staging, Trash stays",
          [n for n in ("Loose landmark", "Cafe", "Beach", "Imp store", "Old store") if n in pby], [])
    check("prune: a product folder under #RLV is in scope", "Steel store" in pby, True)
    check("prune: an ad texture goes, with the reason",
          (pby.get("Zora Dress AD") or {}).get("because"), "ad texture: name says ad (AD)")
    check("prune: a texture named like the store goes",
          (pby.get("SALT & PEPPER") or {}).get("because"), "ad texture: named like the store (landmark Salt & Pepper, The Birdcage)")
    check("prune: textures stay when there are no signs, or a kit's signs",
          [n for n in ("Zora Top Silk", "Pax AD", "Mara Chair ad 1", "Lamp shade ad", "Bowl ad", "Glass")
           if n in pby], [])
    check("prune: a weak sign that shares a word with the product stays",
          "Poster" in pby, False)
    check("prune: why textures were kept is counted",
          (pstats["textures kept: no sign it is an ad"], pstats["textures kept: more than 2 in the folder"],
           pstats["textures kept: kit word in the folder name"], pstats["textures kept: Textures/Kit/HUD subfolder"],
           pstats["textures kept: product has materials"],
           pstats["textures kept: weak ad sign and shares a word with the product name"]),
          (2, 3, 1, 1, 1, 1))
    check("prune: ad words in the product's own name are no evidence",
          ad_evidence("Wall ad", "Zed", [], "Zed Ad Wall"), None)
    check("prune: underscores separate words, digits may follow ad",
          (ad_evidence("Cabinet_ad", "Zed", [], "Zed Cabinet")[0], ad_evidence("Lola Tops - AD3", "Zed", [], "Zed Lola")[0]),
          ("name says ad (ad)", "name says ad (AD3)"))

    # Docs: notecards in product folders go to Notecards > Docs > <Brand>
    dcreators = {k: uuid.uuid4() for k in "ABZ"}
    quill = uuid.uuid4()
    dcats = [cat("d_root", None, "My Inventory", "root_inv"),
             cat("d_nc", "d_root", "Notecards", "notecard"),
             cat("d_docs", "d_nc", "Docs"),
             cat("d_dz", "d_docs", "Zora"),
             cat("d_clo", "d_root", "Clothing", "clothing"),
             cat("d_zora", "d_clo", "Zora"),
             cat("d_z1", "d_zora", "Zora Dress"),
             cat("d_z2", "d_zora", "Zora Top"),
             cat("d_acc", "d_clo", "Accessories"),
             cat("d_kink", "d_acc", "Kink"),
             cat("d_k1", "d_kink", "TRV Pallas Boots"),
             cat("d_k2", "d_kink", "RF Armbinder Pro"),
             cat("d_k3", "d_kink", "Q&Q Bra"),
             cat("d_k4", "d_kink", "TRV mats"),                    # two words: TRV, as the triage joins them
             cat("d_q", "d_root", "Mystery"),                     # no brand key: the creator's name
             cat("d_u", "d_root", "Mystery"),                     # nothing at all: Unsorted
             cat("d_ao", "d_root", "Cosy AO"),                    # an AO's config notecard stays
             cat("d_notes", "d_root", "Recipes"),                 # only notecards: not a product
             cat("d_rlv", "d_root", "#RLV"),
             cat("d_rb", "d_rlv", "Cuffs"),
             cat("d_r1", "d_rb", "Cuffs - Steel"),
             cat("d_r2", "d_rb", "Cuffs - Iron"),
             cat("d_wi", "d_root", "Wardrobe Import"),
             cat("d_w1", "d_wi", "Imp Dress"),
             cat("d_w2", "d_wi", "Imp Top")]

    def ditem(parent, name, creator, atype="object"):
        return item(parent, name, creator, atype)
    ditems = [ditem(k, "%s part" % k, dcreators["A"]) for k in
              ("d_z1", "d_z2", "d_k1", "d_k2", "d_k3", "d_k4", "d_r1", "d_r2", "d_w1", "d_w2")]
    ditems += [ditem("d_q", "Mystery thing", quill), ditem("d_u", "Unknown thing", null)]
    ditems += [ditem("d_ao", "AO stand %d" % i, dcreators["A"], "animatn") for i in range(3)]
    ditems += [ditem("d_z1", "Read me", dcreators["A"], "notecard"), ditem("d_z1", "Care guide", dcreators["Z"], "notecard"),
               ditem("d_z2", "Read me", dcreators["A"], "notecard"), ditem("d_z2", "Read me", dcreators["B"], "notecard"),
               ditem("d_dz", "Care guide", dcreators["Z"], "notecard"),
               ditem("d_k1", "TRV info", dcreators["A"], "notecard"), ditem("d_k4", "Mats info", dcreators["A"], "notecard"), ditem("d_k2", "RF info", dcreators["B"], "notecard"),
               ditem("d_q", "Quill info", quill, "notecard"), ditem("d_u", "Lost info", null, "notecard"),
               ditem("d_ao", "AO config", dcreators["A"], "notecard"),
               ditem("d_notes", "Soup", dcreators["A"], "notecard"), ditem("d_notes", "Bread", dcreators["A"], "notecard"),
               ditem("d_r1", "Steel info", dcreators["A"], "notecard"),
               ditem("d_w1", "Imp info", dcreators["A"], "notecard")]
    dinv = Inventory(dcats, ditems, creators={str(quill): "Quill Resident (Quillon)"})
    dstats = Counter()
    drows = docs_plan(dinv, dstats)
    dmoves = {(r["item_name"], r["from"].split(" > ")[-1]): r for r in drows if r["kind"] == "item_move"}
    check("docs: notecards in product folders move to Notecards > Docs > <brand>",
          sorted((r["item_name"], r["to"]) for r in drows if r["kind"] == "item_move"),
          sorted([("Lost info", "Notecards > Docs > Unsorted"), ("Mats info", "Notecards > Docs > TRV"),
                  ("Quill info", "Notecards > Docs > Quill"), ("TRV info", "Notecards > Docs > TRV"),
                  ("RF info", "Notecards > Docs > RF"),
                  ("Read me", "Notecards > Docs > Zora"), ("Read me", "Notecards > Docs > Zora"),
                  ("Steel info", "Notecards > Docs > Unsorted")]))    # "Cuffs" is a category word
    check("docs: a brand folder names the brand, the flat Kink folder uses each product's own key",
          (dmoves[("Read me", "Zora Dress")]["to"], dmoves[("TRV info", "TRV Pallas Boots")]["to"],
           dmoves[("RF info", "RF Armbinder Pro")]["to"]),
          ("Notecards > Docs > Zora", "Notecards > Docs > TRV", "Notecards > Docs > RF"))
    check("docs: no brand key falls back to the creator, then Unsorted",
          (dmoves[("Quill info", "Mystery")]["to"], dmoves[("Lost info", "Mystery")]["to"]),
          ("Notecards > Docs > Quill", "Notecards > Docs > Unsorted"))
    check("docs: a product under #RLV is in scope, Wardrobe Import is not",
          ("Steel info" in {k[0] for k in dmoves}, "Imp info" in {r["item_name"] for r in drows}), (True, False))
    check("docs: an AO's config and a folder of only notecards stay",
          [r["item_name"] for r in drows if r["item_name"] in ("AO config", "Soup", "Bread")], [])
    check("docs: the AO exception is counted", sum(dstats.values()), 1)
    dtrash = [(r["item_name"], r["from"].split(" > ")[-1]) for r in drows if r["kind"] == "item_trash"]
    check("docs: the same name and creator twice for one brand, or already filed there: the extra is trash",
          sorted(dtrash), [("Care guide", "Zora Dress"), ("Read me", "Zora Top")])
    check("docs: the first copy is kept, a different creator's is not a duplicate",
          (("Read me", "Zora Dress") in dmoves, sorted(r["from"].split(" > ")[-1] for r in drows
                                                       if r["item_name"] == "Read me" and r["kind"] == "item_move")),
          (True, ["Zora Dress", "Zora Top"]))
    check("docs: the reason says what was not compared",
          [r["because"] for r in drows if r["kind"] == "item_trash" and r["item_name"] == "Read me"],
          ["duplicate notecard (same name and creator already under Notecards > Docs > Zora; "
           "content not compared, the export has no asset id)"])
    check("docs: rows are valid actions", write_action_plan(drows, os.devnull) is drows, True)

    # docs brand rules: 1 brand folder, 2 key + map, 4 creator, 5 never generic, 6 one spelling
    smith, mzc, acme = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bcats = [cat("b_root", None, "My Inventory", "root_inv"),
             cat("b_nc", "b_root", "Notecards", "notecard"),
             cat("b_docs", "b_nc", "Docs"),
             cat("b_dacme", "b_docs", "ACME"),
             cat("b_clo", "b_root", "Clothing", "clothing"),
             cat("b_zora", "b_clo", "Zora"),
             cat("b_z1", "b_zora", "Zora Dress"), cat("b_z2", "b_zora", "Zora Top"),
             cat("b_z3", "b_zora", "Midnight Gown"),             # rule 1: the brand folder, not "Midnight"
             cat("b_bw", "b_clo", "[BW] Bramblewick Rings"),    # rule 2: key BW, mapped
             cat("b_hood", "b_clo", "Hoods"),                   # rule 5: generic, so the creator
             cat("b_hood_cat", "b_clo", "Latex"),
             cat("b_ac", "b_clo", "[Acme] Collar"),             # rule 6: reuse the existing ACME
             cat("b_m1", "b_clo", "[MOSAIC] Fatpack"),            # rule 6: MOSAIC / MOSAIC.mods, one creator
             cat("b_m2", "b_clo", "[MOSAIC.mods] Soft Thighs"),
             cat("b_lat", "b_hood_cat", "Latex Catsuit Pro")]   # rule 5: a "Latex" folder is no brand
    bitems = [item(k, "%s part" % k, smith) for k in ("b_z1", "b_z2", "b_z3", "b_bw", "b_hood", "b_ac", "b_lat")]
    bitems += [item(k, "%s part" % k, mzc) for k in ("b_m1", "b_m2")]
    bitems += [item(k, "Read me %s" % k, mzc if k.startswith("b_m") else smith, "notecard")
               for k in ("b_z3", "b_bw", "b_hood", "b_ac", "b_m1", "b_m2", "b_lat")]
    binv = Inventory(bcats, bitems, creators={str(smith): "Gearsmith Resident (Gs)", str(mzc): "Mosaic Maker Resident",
                                              str(acme): "Acme Resident"})
    def dest_of(inv_, brands_=None):
        return {r["from"].split(" > ")[-1]: r["to"].replace("Notecards > Docs > ", "")
                for r in docs_plan(inv_, None, brands_) if r["kind"] == "item_move"}
    bd = dest_of(binv, {"bw": "Bramblewick"})
    check("docs brand 1: a brand folder above the product names the brand", bd["Midnight Gown"], "Zora")
    check("docs brand 2: the product's brand key, through the map's brands",
          (bd["[BW] Bramblewick Rings"], dest_of(binv)["[BW] Bramblewick Rings"]), ("Bramblewick", "BW"))
    check("docs brand 4: no usable brand: the creator, no Resident, no parentheses",
          (bd["Hoods"], clean_creator_name("Jaywalker Resident (Jessica)"),
           clean_creator_name("Mosaic Maker Resident")), ("Gearsmith", "Jaywalker", "Mosaic Maker"))
    check("docs brand 5: category words are never a brand",
          (bd["Latex Catsuit Pro"], [w for w in ("Accessories", "Body", "Collars", "Hoods", "Hosiery", "Shapes",
           "Bottoms", "Tops", "Chokers", "Corsets", "Dresses", "Gadgets", "Latex", "Lingerie", "Mittens",
           "Rubber", "Straitjackets", "Yokes", "Shoes", "Boots", "Heels", "Catsuits", "Outfits", "Misc",
           "Other", "Unsorted", "Docs", "Documentation", "Notecards", "Boxed", "Unpacked")
           if not is_generic_brand(w)], is_generic_brand("Zora")), ("Gearsmith", [], False))
    check("docs brand 6: an existing Docs folder's spelling is reused, MOSAIC and MOSAIC.mods by one creator merge",
          (bd["[Acme] Collar"], bd["[MOSAIC] Fatpack"], bd["[MOSAIC.mods] Soft Thighs"]),
          ("ACME", "MOSAIC.mods", "MOSAIC.mods"))
    check("docs: no notecard already under Notecards is touched",
          [r for r in drows if r["from"].startswith("Notecards")], [])
    check("brand spelling: one per brand, junk keys skipped",
          (own_brand_key("Cloak BOXED"), own_brand_key("[Add Me] Squish Belt"), own_brand_key("Bag PFS Ponysuit"),
           own_brand_key("TRV Pallas Boots")), ("Cloak", "Squish Belt", "", "TRV"))

    # Dedupe: same-name product folders, by rule or for review
    zc = uuid.uuid4()
    qcats = [cat("q_root", None, "My Inventory", "root_inv"),
             cat("q_inbox", "q_root", "Received Items", "inbox"),
             cat("q_clo", "q_root", "Clothing", "clothing"),
             cat("q_clothes", "q_clo", "Clothes"),
             cat("q_zed", "q_clothes", "Zed"),
             cat("q_yar", "q_clothes", "Yar"),
             cat("q_obj", "q_root", "Objects", "object"),
             cat("q_tu", "q_obj", "To Unpack"),
             cat("q_tuz", "q_tu", "Zed"),
             cat("q_nts", "q_obj", "Need to Sort"),
             cat("q_rlv", "q_root", "#RLV"),
             cat("q_rz", "q_rlv", ".Zed")]
    qitems = []

    def qprod(parent, name, parts, atype="clothing"):
        key = "q_%d" % len(qcats)
        qcats.append(cat(key, parent, name))
        qitems.extend(item(key, p, zc, atype) for p in parts)
        return key

    def parts(tag, n=3):
        return ["Zed %s %d" % (tag, i) for i in range(n)]
    # the brand folders need a few products of their own
    for tag in ("Ring", "Sock", "Glove"):
        qprod("q_zed", "Zed " + tag, parts(tag))
        qprod("q_rz", "Zed " + tag + " RLV", parts(tag + "r"))
    for tag in ("Hood", "Vest"):
        qprod("q_yar", "Yar " + tag, parts(tag))
    qprod("q_tuz", "Zed Gag (box)", ["Zed Gag (box)"], "object")
    for tag in ("A", "B"):
        qprod("q_nts", "Misc " + tag, parts(tag))
    # A: a box in To Unpack and the opened copy in the brand folder
    qprod("q_zed", "Zed Dress", parts("Dress"))
    qprod("q_tuz", "Zed Dress", ["Zed Dress (box)"], "object")
    # B: twins in one brand folder, names differing only in case and spacing
    qprod("q_zed", "Zed Top", parts("Top"))
    qprod("q_zed", "Zed Top", parts("Top"))
    qprod("q_zed", "Zed  Scarf", parts("Scarf"))
    qprod("q_zed", "zed scarf", parts("Scarf"))
    # C: the brand-home copy and one left in Need to Sort
    qprod("q_zed", "Zed Skirt", parts("Skirt"))
    qprod("q_nts", "Zed Skirt", parts("Skirt"))
    # D: identical copies in two brand homes: a look
    qprod("q_zed", "Zed Cape", parts("Cape"))
    qprod("q_yar", "Zed Cape", parts("Cape"))
    # E: same name, different contents: versions
    qprod("q_zed", "Zed Hat", parts("Hat", 3))
    qprod("q_zed", "Zed Hat", parts("Hat", 5))
    # F: an identical copy under #RLV is shown and never trashed
    qprod("q_zed", "Zed Belt", parts("Belt"))
    qprod("q_rz", "Zed Belt", parts("Belt"))
    # G: the box sits in Received Items: protected
    qprod("q_zed", "Zed Boots", parts("Boots"))
    qprod("q_inbox", "Zed Boots", ["Zed Boots (box)"], "object")
    qinv = Inventory(qcats, qitems)
    qgroups = dedupe_groups(qinv)

    def norm_name(g):
        return " ".join(g["members"][0].name.split()).casefold()
    qby = {norm_name(g): g for g in qgroups}
    check("dedupe: groups are same-name product folders, names normalised",
          sorted(qby), ["zed belt", "zed boots", "zed cape", "zed dress", "zed hat", "zed scarf", "zed skirt", "zed top"])
    check("dedupe: box+opened, identical and versions are told apart",
          {n: g["kind"] for n, g in qby.items()},
          {"zed dress": "box+opened", "zed top": "identical", "zed scarf": "identical",
           "zed skirt": "identical", "zed cape": "identical", "zed hat": "versions",
           "zed belt": "identical", "zed boots": "box+opened"})
    qplan = dedupe_plan(qinv, qgroups)
    check("dedupe: the plan trashes the box, a twin of each pair, the copy in Need to Sort, nothing else",
          sorted((" ".join(r["item_name"].split()).casefold(), r["from"]) for r in qplan),
          [("zed dress", "Objects > To Unpack > Zed"), ("zed scarf", "Clothing > Clothes > Zed"),
           ("zed skirt", "Objects > Need to Sort"), ("zed top", "Clothing > Clothes > Zed")])
    check("dedupe: rows are folder_trash with no destination and the folder's id and its parent's",
          ({(r["kind"], r["to"], r["asset_type"]) for r in qplan},
           {r["item_id"] for r in qplan} <= {str(f.id) for g in qgroups for f in g["trash"]},
           all(r["from_id"] for r in qplan)), ({("folder_trash", "", "Folder")}, True, True))

    def names(g):
        return [qinv.path(f) for f in g["trash"]]
    check("dedupe: a box in To Unpack goes, the opened copy stays",
          names(qby["zed dress"]), ["Objects > To Unpack > Zed > Zed Dress"])
    check("dedupe: twins in one brand folder keep one",
          [(len(qby[n]["trash"]), qby[n]["keep"] is not None) for n in ("zed top", "zed scarf")], [(1, True), (1, True)])
    check("dedupe: the brand-home copy is kept, the Need to Sort one goes",
          (qinv.path(qby["zed skirt"]["keep"]), names(qby["zed skirt"])),
          ("Clothing > Clothes > Zed > Zed Skirt", ["Objects > Need to Sort > Zed Skirt"]))
    check("dedupe: copies in two brand homes and versions have no default",
          (qby["zed cape"]["trash"], qby["zed cape"]["default"], qby["zed hat"]["trash"], qby["zed hat"]["default"]),
          ([], "review: copies in 2 different brand homes", [], "review: no rule decides it"))
    check("dedupe: nothing under #RLV or Received Items is ever trashed",
          [qinv.path(f) for g in qgroups for f in g["trash"]
           if qinv.path(f).startswith("#RLV") or qinv.path(f).startswith("Received Items")], [])
    check("dedupe: protected copies are shown as members, with the reason there is no default",
          (len(qby["zed belt"]["members"]), qby["zed belt"]["default"], len(qby["zed boots"]["members"]),
           qby["zed boots"]["default"]),
          (2, "review: the other copies are in protected areas", 2, "review: the box is in a protected area"))
    # Generic part folders of different products are never duplicates
    gcats = [cat("g_root", None, "My Inventory", "root_inv"),
             cat("g_bp", "g_root", "Body Parts", "bodypart"),
             cat("g_heads", "g_bp", "Heads"),
             cat("g_logan", "g_heads", "Logan"),
             cat("g_bodies", "g_bp", "Bodies"),
             cat("g_lara", "g_bodies", "LaraX Body"),
             cat("g_reb", "g_bodies", "Reborn Body"),
             cat("g_obj", "g_root", "Objects", "object"),
             cat("g_boxed", "g_obj", "Boxed Items"),
             cat("g_shoes", "g_obj", "Pumpkin Shoes"),
             cat("g_nts", "g_obj", "Need to Sort"),
             cat("g_q", "g_heads", "Quasar Head")]
    gitems = []

    def gprod(parent, name, parts_, atype="clothing"):
        key = "g_%d" % len(gcats)
        gcats.append(cat(key, parent, name))
        gitems.extend(item(key, p_, zc, atype) for p_ in parts_)
        return key
    gprod("g_logan", "Eyes", ["Logan eye %d" % i for i in range(3)])
    gprod("g_boxed", "Eyes", ["Eyes (box)"], "object")
    for parent in ("g_lara", "g_reb", "g_logan", "g_shoes"):
        gprod(parent, "HUDs", ["%s hud" % parent], "object")
    gprod("g_lara", "Eyes", ["Lara eye"], "object")
    # a real product pair that is not the same product: downgraded to review
    gprod("g_obj", "Marble Statue", ["Marble Statue %d" % i for i in range(3)], "object")
    gprod("g_nts", "Marble Statue", ["Completely unrelated thing"], "object")
    # a real box+opened pair inside a Body Parts head: never default-trashed
    gprod("g_heads", "Quasar Mask", ["Quasar Mask %d" % i for i in range(3)])
    gprod("g_heads", "Quasar Mask", ["Quasar Mask (box)"], "object")
    # a real box+opened pair elsewhere still goes
    gprod("g_obj", "Ember Lamp", ["Ember Lamp %d" % i for i in range(3)], "object")
    gprod("g_nts", "Ember Lamp", ["Ember Lamp (box)"], "object")
    ginv = Inventory(gcats, gitems)
    ggroups = dedupe_groups(ginv)
    gby = {" ".join(g["members"][0].name.split()).casefold(): g for g in ggroups}
    check("dedupe: generic part folders (HUDs, Eyes) of different products never group",
          sorted(gby), ["ember lamp", "marble statue", "quasar mask"])
    check("dedupe: a box and an opened folder that are not plausibly one product are reviewed, not trashed",
          (gby["marble statue"]["trash"], gby["marble statue"]["default"].startswith("review")), ([], True))
    check("dedupe: nothing under Body Parts bodies or heads is default-trashed",
          [ginv.path(f) for g in ggroups for f in g["trash"] if f.zone == "Body Parts"], [])
    check("dedupe: a plausible box+opened pair is still trashed",
          [ginv.path(f) for f in gby["ember lamp"]["trash"]], ["Objects > Need to Sort > Ember Lamp"])
    check("dedupe: short and generic names are not candidates",
          [dedupe_candidate(ginv, f) for f in ginv.folders.values() if f.name in ("HUDs", "Eyes")],
          [False] * 7)
    with tempfile.TemporaryDirectory() as tmp:
        rpath = os.path.join(tmp, "review.tsv")
        write_dedupe_review(qinv, qgroups, rpath)
        with open(rpath, newline="", encoding="utf-8-sig") as fh:
            review = list(csv.reader(fh, delimiter="\t"))
    check("dedupe: the review file is numbered per group with the four columns",
          (review[0][:3], [r[0] for r in review[1:]] == [str(n) for n in range(1, len(qgroups) + 1)],
           all(len(r) == 4 for r in review)), (["n", "kind", "default"], True, True))
    check("dedupe: the review lists folders with item counts and what happens to them",
          [r[3] for r in review[1:] if r[1] == "box+opened" and "Zed Dress" in r[3]],
          ["Clothing > Clothes > Zed > Zed Dress [3 items, UNPACKED] || "
           "Objects > To Unpack > Zed > Zed Dress [1 items, BOXED] -> trash"])

    # --- RLV planner: short names, types, roots, collisions, in-place ------------
    sn = rlv_short_name
    check("rlv names: brand prefix, box word and version go, title case of a lower-case name",
          sn("TRV helene hood box V2", ["TRV"]), "Helene Hood")
    check("rlv names: product words stay, brand prefix goes", sn("MKR Vermilion thigh cuffs v5", ["MKR"]),
          "Vermilion Thigh Cuffs")
    check("rlv names: brand and separator before the product", sn("TRV - Compact Armbinder", ["TRV"]),
          "Compact Armbinder")
    check("rlv names: a brand that would leave only a type word stays", sn("Desire Tights", ["Desire"]),
          "Desire Tights")
    check("rlv names: [Tag] prefix, brand, size words and the dash go",
          sn("[BW] Bramblewick - Bikini - Brianna - Legacy", ["Bramblewick"]), "Bikini Brianna")
    check("rlv names: body names, pack words, backslash separator",
          sn("ALDER Josie high waist shorts \\ Fullpack (Maitreya Petite)", ["ALDER"]), "Josie High Waist Shorts")
    check("rlv names: version and (f) marks go", sn("GLINT - Hera Cuffs (f) - Megapack (v2.0.0)", ["GLINT"]), "Hera Cuffs")
    check("rlv names: the version stays when asked for (a collision)",
          sn("GLINT - Hera Cuffs (v2.0.0)", ["GLINT"], keep_version=True), "Hera Cuffs (v2.0.0)")
    check("rlv names: / and \\ inside a word become 'and'", sn("groins/bulge strap", []), "Groins and Bulge Strap")
    check("rlv names: a tag that holds the whole name is unwrapped, ALL CAPS is title-cased",
          sn("TRV [VESTA GASMASK FATPACK]", ["TRV"]), "Vesta Gasmask")
    check("rlv names: no leading dot, no trailing dots, spaces collapsed",
          sn(".Foo   Bar Hood...", []), "Foo Bar Hood")
    check("rlv names: at most 60 characters", len(sn("Alpha " * 30, [])) <= RLV_NAME_MAX, True)
    check("rlv names: a bracket inside a word is not a tag ([H]arsh)", sn("[H]arsh Female Straitjacket", []),
          "Harsh Straitjacket")
    check("rlv names: a dangling preposition after a body name goes", sn("Cushy Dress for Kemono", ["Cushy"]),
          "Cushy Dress")
    nt = rlv_name_type
    check("rlv types: a latex hood is a hood (restraints win)", nt("Latex Hood"), ("Restraints", "Hood"))
    check("rlv types: a corset dress is a corset", nt("Corset Dress"), ("Restraints", "Corset"))
    check("rlv types: a dress is a dress", nt("Maya Dress"), ("Clothes", "Dress"))
    check("rlv types: the last clothing word is the noun", nt("Sweater Dress"), ("Clothes", "Dress"))
    check("rlv types: several clothes with 'and' make an outfit", nt("Top and Skirt"), ("Clothes", "Outfit"))
    check("rlv types: several restraints in one set are not guessed", nt("Collar & Leash Set"),
          ("MULTI", "Collar, Leash"))
    check("rlv types: cuffs by limb", (nt("Vermilion thigh cuffs"), nt("wrist cuffs"), nt("Hera cuffs")),
          (("Restraints", "Cuffs - Legs"), ("Restraints", "Cuffs - Arms"), ("Restraints", "Cuffs - Arms")))
    check("rlv types: tights are restraints in a restraint folder, hosiery elsewhere",
          (nt("Desire Tights", True), nt("Sheer Tights")), (("Restraints", "Tights"), ("Clothes", "Hosiery")))
    check("rlv types: nipple rings are a piercing, not jewellery", nt("Nipple Rings"), ("Accessories", "Piercing"))
    check("rlv types: no keyword, no type", nt("Mystery Thing"), None)
    check("rlv types: a bare belt is a chastity belt only among restraints",
          (nt("Squish Belt", True), nt("Squish Belt")), (("Restraints", "Chastity Belt"), ("Accessories", "Other")))
    check("rlv types: Marketplace categories (deepest segment first)",
          (rlv_category(["Apparel", "Women's", "Women's Dresses"]),
           rlv_category(["Avatar Accessories", "BDSM Accessories", "BDSM Collars"]),
           rlv_category(["Apparel", "Women's", "Women's Tops", "Women's Corsets"]),
           rlv_category(["Avatar Accessories", "Hair", "Women's Long Hair"])),
          (("Clothes", "Dress", False), ("Restraints", "Collar", False), ("Restraints", "Corset", False),
           ("OUT", None, False)))
    check("rlv types: a Marketplace listing of the same name gives the type; one word of a longer title does not",
          (rlv_listing_type(None, icr.Folder({"cat_id": uuid.uuid4(), "name": "Zed - Moss Tea"}), ["Zed"],
                            {"zed": {"listings": [["Zed Moss Tea", ["Avatar Accessories", "BDSM Accessories",
                                                                    "BDSM Collars"]]]}}, None)[:2],
           rlv_listing_type(None, icr.Folder({"cat_id": uuid.uuid4(), "name": "Zed - Moss"}), ["Zed"],
                            {"zed": {"listings": [["Zed Moss Thigh High Boots", ["Apparel", "Women's",
                                                                                 "Women's Footwear"]]]}}, None)),
          (("Restraints", "Collar"), None))
    check("rlv layout: canonical destinations",
          [rlv_canonical_dest(p) for p in ("#RLV > Restraints > Hood", "#RLV > Restraints > Hoods",
                                           "#RLV > Utilities", "#RLV > Utilities > Whim",
                                           "#RLV > Clothes > Dress", "#RLV > Clothing > Dress",
                                           "#RLV > Restraints > Cuffs - Legs", "Clothing > Clothes")],
          [True, False, True, False, True, False, True, False])

    rcats = [cat("root", None, "My Inventory", "root_inv"),
             cat("clothing", "root", "Clothing", "clothing"),
             cat("kink", "clothing", "Kink"),
             cat("k1", "kink", "TRV helene hood box V2"),
             cat("k2", "kink", "TRV - Compact Armbinder"), cat("k2h", "k2", "HUDs"),
             cat("k3", "kink", "Q&Q - Posture Collar Pro"),
             cat("k4", "kink", "{ps} Posture Collar Pro"),
             cat("k5", "kink", "Mystery Thing"),
             cat("k6", "kink", "TRV Mask (boxed)"),
             cat("k7", "kink", "MKR Collar DEMO"),
             cat("k8", "kink", "Fancy Sleeve HUD"),
             cat("k9", "kink", "Whim HUD"),
             cat("clothes", "clothing", "Clothes"),
             cat("osm", "clothes", "ALDER"),
             cat("o1", "osm", "ALDER - Paige Tee (Fatpack)"),
             cat("o2", "osm", "ALDER - Della Dress (Fatpack)"),
             cat("rlv", "root", "#RLV"),
             cat("rr", "rlv", "Restraints"),
             cat("rh", "rr", "Hood"),
             cat("rh1", "rh", "Persis"),
             cat("rhs", "rr", "Hoods"),
             cat("rhs1", "rhs", "Simplicity Hood"),
             cat("rc", "rlv", "Clothing"),
             cat("rcd", "rc", "Dresses"),
             cat("rcd1", "rcd", "Frida Dress"),
             cat("rid", "rlv", "~vendor"),
             cat("rid1", "rid", "Prison Suit")]
    rc = uuid.uuid4()
    ritems = [item("k1", "TRV Helene hood (skull)", rc), item("k1", "Helene alpha", rc, "clothing"),
              item("k2", "Compact Armbinder (l upper arm)", rc), item("k2", "Compact Armbinder (r upper arm)", rc),
              item("k2h", "Armbinder HUD", rc),
              item("k3", "Q&Q posture collar (neck)", rc), item("k3", "posture alpha", rc, "clothing"),
              item("k4", "ps posture collar (neck)", rc), item("k4", "ps alpha", rc, "clothing"),
              item("k5", "Mystery a", rc, "clothing"), item("k5", "Mystery b", rc, "clothing"),
              item("k6", "TRV Mask Box", rc),
              item("k7", "Collar a", rc, "clothing"), item("k7", "Collar b", rc, "clothing"),
              item("k8", "Fancy Sleeve HUD v1", rc), item("k8", "Fancy Sleeve Applier", rc),
              item("k9", "Whim HUD", rc), item("k9", "Whim installer", rc),
              item("o1", "Paige Tee", rc, "clothing"), item("o1", "Paige Tee alpha", rc, "clothing"),
              item("o2", "Della Dress", rc, "clothing"), item("o2", "Della Dress alpha", rc, "clothing"),
              item("rh1", "Persis hood (skull)", rc), item("rh1", "Persis alpha", rc, "clothing"),
              item("rhs1", "Simplicity Hood (skull)", rc), item("rhs1", "Simplicity alpha", rc, "clothing"),
              item("rcd1", "Frida Dress", rc, "clothing"), item("rcd1", "Frida alpha", rc, "clothing"),
              item("rid1", "Prison Suit", rc, "clothing"), item("rid1", "Prison alpha", rc, "clothing")]
    rinv = Inventory(rcats, ritems)
    rstats = Counter()
    rrows, rrev = rlv_plan(rinv, {"brands": {}}, {}, rstats)
    rby = {r["product_folder"]: r for r in rrows}
    check("rlv plan: a hood in the Kink bucket goes to Restraints > Hood, brand and box word stripped",
          (rby["TRV helene hood box V2"]["to"], rby["TRV helene hood box V2"]["rename_to"]),
          ("#RLV > Restraints > Hood", "Helene Hood"))
    check("rlv plan: the because names the type, the signal and the original name",
          rby["TRV helene hood box V2"]["because"],
          "Restraints > Hood: name keyword; name 'TRV helene hood box V2'")
    check("rlv plan: a part subfolder (HUDs) moves with its product, not as a product",
          ("TRV - Compact Armbinder" in rby, "HUDs" in rby, rby["TRV - Compact Armbinder"]["rename_to"]),
          (True, False, "Compact Armbinder"))
    check("rlv plan: clothes go to Clothes > Type with the brand stripped",
          [(rby[n]["to"], rby[n]["rename_to"]) for n in ("ALDER - Paige Tee (Fatpack)", "ALDER - Della Dress (Fatpack)")],
          [("#RLV > Clothes > Top", "Paige Tee"), ("#RLV > Clothes > Dress", "Della Dress")])
    check("rlv plan: a legacy #RLV folder is re-filed under the new spelling",
          [(rby[n]["from"], rby[n]["to"], rby[n]["rename_to"]) for n in ("Simplicity Hood", "Frida Dress")],
          [("#RLV > Restraints > Hoods", "#RLV > Restraints > Hood", ""),
           ("#RLV > Clothing > Dresses", "#RLV > Clothes > Dress", "")])
    check("rlv plan: a product already at a canonical path with a canonical name has no row",
          ("Persis" in rby, rstats["already in place"]), (False, 1))
    check("rlv plan: vendor-managed ~ folders are never touched",
          ("Prison Suit" in rby, any(r["product_folder"] == "Prison Suit" for r in rrev)), (False, False))
    check("rlv plan: a RLV tool by name goes to Utilities",
          (rby["Whim HUD"]["to"], rby["Whim HUD"]["rename_to"]), ("#RLV > Utilities", "Whim"))
    check("rlv plan: every row may create under #RLV and names its ids",
          (all(r["allow_protected"] == "yes" for r in rrows), all(r["folder_id"] and r["from_id"] for r in rrows),
           set(rrows[0]) == set(PLAN_FIELDS)), (True, True, True))
    check("rlv plan: new destinations are marked, existing ones are not",
          (rby["TRV helene hood box V2"]["creates_folder"], rby["Simplicity Hood"]["creates_folder"],
           rby["Frida Dress"]["creates_folder"]), ("", "", "yes"))
    check("rlv plan: two products with one short name get the brand in parentheses, both",
          sorted((r["product_folder"], r["rename_to"]) for r in rrows if "Posture" in r["product_folder"]),
          [("Q&Q - Posture Collar Pro", "Posture Collar Pro (Q&Q)"), ("{ps} Posture Collar Pro", "Posture Collar Pro (ps)")])
    rrv = {r["product_folder"]: r for r in rrev}
    check("rlv review: no confident type, box, demo and HUD-only products are reviewed, not planned",
          [(n in rby, rrv[n]["kind"], rrv[n]["why"].split(":")[0]) for n in
           ("Mystery Thing", "TRV Mask (boxed)", "MKR Collar DEMO", "Fancy Sleeve HUD")],
          [(False, "review", "no confident type"), (False, "review", "box"), (False, "review", "demo"),
           (False, "review", "HUD/applier only")])
    check("rlv review: numbered 1..n, one line per product, sorted by proposed_to then name",
          ([r["n"] for r in rrev] == list(range(1, len(rrev) + 1)),
           [(r["proposed_to"], r["short_name"].casefold()) for r in rrev] ==
           sorted((r["proposed_to"], r["short_name"].casefold()) for r in rrev),
           len(rrev) == len(rrows) + 4), (True, True, True))
    check("rlv review: the plan rows say 'plan' and the others 'review'",
          Counter(r["kind"] for r in rrev), Counter({"plan": len(rrows), "review": 4}))
    rcol_cats = [cat("root", None, "My Inventory", "root_inv"), cat("rlv", "root", "#RLV"),
                 cat("rr", "rlv", "Restraints"), cat("rh", "rr", "Hood"), cat("rh1", "rh", "Helene Hood"),
                 cat("clothing", "root", "Clothing", "clothing"), cat("c1", "clothing", "TRV - Helene Hood"),
                 cat("c2", "clothing", "TRV - Helene Hood (copy)")]
    rcol_items = [item("rh1", "Helene hood", rc), item("rh1", "Helene alpha", rc, "clothing"),
                  item("c1", "Helene hood (skull)", rc), item("c1", "Helene alpha", rc, "clothing"),
                  item("c2", "Helene hood (skull)", rc), item("c2", "Helene alpha", rc, "clothing")]
    crows, crev = rlv_plan(Inventory(rcol_cats, rcol_items), {}, {}, Counter())
    check("rlv collision: a newcomer cannot take the name of a product in place; the same brand is reviewed",
          sorted((r["kind"], r["product_folder"]) for r in crev if r["why"].startswith("name collision")),
          [("review", "TRV - Helene Hood"), ("review", "TRV - Helene Hood (copy)")])
    with tempfile.TemporaryDirectory() as tmp:
        rpath, rrpath = os.path.join(tmp, "rlv_plan.csv"), os.path.join(tmp, "rlv_review.tsv")
        write_folder_plan(rrows, rpath)
        write_rlv_review(rrev, rrpath)
        with open(rpath, newline="", encoding="utf-8-sig") as fh:
            rhead = fh.readline().strip()
        with open(rrpath, newline="", encoding="utf-8-sig") as fh:
            rreview = list(csv.reader(fh, delimiter="\t"))
    check("rlv files: the plan has the folder-plan header, the review the seven columns",
          (rhead, rreview[0], len(rreview) - 1),
          (",".join(PLAN_FIELDS), ["n", "kind", "product_folder", "from", "proposed_to", "short_name", "why"],
           len(rrev)))

    # The prune / docs / dedupe commands run end to end on an export file
    with tempfile.TemporaryDirectory() as tmp:
        cpath = os.path.join(tmp, "hangar-export-test.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(icr.synthetic_export()[0], fh)
        shown = {}
        for job in ("prune", "docs"):
            screen = io.StringIO()
            plan_command([job, cpath, os.path.join(tmp, job + ".csv")], out=screen)
            shown[job] = screen.getvalue().split(" ")[0:2]
        screen = io.StringIO()
        plan_command(["dedupe", cpath, os.path.join(tmp, "r.tsv"), os.path.join(tmp, "d.csv")], out=screen)
        with open(os.path.join(tmp, "d.csv"), encoding="utf-8-sig") as fh:
            header = fh.readline().strip()
        written = sorted(os.listdir(tmp))
    check("commands: prune, docs and dedupe write their files and report",
          (shown, header, written, screen.getvalue().startswith("Review list")),
          ({"prune": ["Plan", "written"], "docs": ["Plan", "written"]},
           "kind,item_name,asset_type,from,to,because,item_id,from_id",
           ["d.csv", "docs.csv", "hangar-export-test.json", "prune.csv", "r.tsv"], True))
    with tempfile.TemporaryDirectory() as tmp:
        cpath = os.path.join(tmp, "hangar-export-test.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(icr.synthetic_export()[0], fh)
        screen = io.StringIO()
        plan_command(["rlv", cpath, os.path.join(tmp, "p.csv"), os.path.join(tmp, "r.tsv"),
                      "--store-cache", os.path.join(tmp, "none.json")], out=screen)
        with open(os.path.join(tmp, "p.csv"), encoding="utf-8-sig") as fh:
            rlv_header = fh.readline().strip()
        with open(os.path.join(tmp, "r.tsv"), encoding="utf-8-sig") as fh:
            rlv_review_header = fh.readline().strip()
    check("commands: rlv writes a folder plan and a review list and reports",
          (rlv_header, rlv_review_header, screen.getvalue().startswith("Plan written")),
          (",".join(PLAN_FIELDS), "	".join(REVIEW_FIELDS), True))
    check("commands: the four jobs are dispatched before the cache arguments", PLAN_JOBS,
          ("prune", "docs", "dedupe", "rlv", "empties"))

    # empties plan
    ecats = [cat("root", None, "My Inventory", "root_inv"), cat("trash", "root", "Trash", "trash"),
             cat("rlv", "root", "#RLV"), cat("ecl", "rlv", "Clothing"), cat("ecl1", "ecl", "Dresses"),
             cat("ecl2", "ecl", "Tops"), cat("ers", "rlv", "Restraints"), cat("ecuff", "ers", "Cuffs"),
             cat("ehood", "ers", "Hoods"), cat("ehp", "ehood", "Ohnus Hood"), cat("ehd", "ehp", "Docs"),
             cat("ehk", "ehp", "Modkit"), cat("ev", "rlv", "~vendor"), cat("ev1", "ev", "Empty part"),
             cat("elk", "rlv", "Linked"), cat("eloose", "root", "Loose"), cat("eloose1", "eloose", "Nothing")]
    eitems = [item("ehp", "Hood", rc), item("elk", "Link", rc, "link")]
    einv = Inventory(ecats, eitems)
    erows = empties_plan(einv)
    check("empties: the topmost empty folder of each subtree, not its children",
          sorted((r["item_name"], r["from"]) for r in erows),
          [("Clothing", "#RLV"), ("Cuffs", "#RLV > Restraints"), ("Docs", "#RLV > Restraints > Hoods > Ohnus Hood"),
           ("Modkit", "#RLV > Restraints > Hoods > Ohnus Hood")])
    check("empties: ~ folders, folders holding a link or an item, and the roots are never listed",
          [r["item_name"] for r in erows if r["item_name"] in ("~vendor", "Empty part", "Linked", "Hoods", "Ohnus Hood", "#RLV")],
          [])
    check("empties: another root can be asked for; rows are folder_trash with ids",
          [(r["kind"], r["item_name"], bool(r["item_id"] and r["from_id"])) for r in empties_plan(einv, ("Loose",))],
          [("folder_trash", "Nothing", True)])

    failed = [(l, g, w) for l, g, w in checks if g != w]
    for label, got, want in failed:
        sys.stderr.write("%s: got %r, want %r\n" % (label, got, want))
    if failed:
        sys.exit("self-test FAILED (%d of %d checks)" % (len(failed), len(checks)))
    print("triage self-test passed (%d checks)" % len(checks))


# --------------------------------------------------------------------------

PLAN_JOBS = ("prune", "docs", "dedupe", "rlv", "empties")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in PLAN_JOBS:
        plan_command(sys.argv[1:])
        return
    parser = argparse.ArgumentParser(
        description="Preview-only inventory triage from a viewer inventory cache.")
    parser.add_argument("path", nargs="?", help="the inventory cache file, or the cache folder")
    parser.add_argument("--map", default="triage_map.json",
                        help="where decisions are saved (default: triage_map.json)")
    parser.add_argument("--list", type=int, nargs="?", const=30, metavar="N",
                        help="show the groups and suggestions without asking anything")
    parser.add_argument("--plan", metavar="CSV", help="write the plan of folder moves to a CSV file")
    parser.add_argument("--fetch-stores", metavar="CSV",
                        help="fetch the Marketplace store pages listed in the store CSV "
                        "(paced to avoid a block; cached in --store-cache)")
    parser.add_argument("--cookie-file", metavar="TXT",
                        help="a file holding the Cookie header of your signed-in Marketplace "
                        "browser session, so adult listings are included; keep it private")
    parser.add_argument("--delay", type=float, default=None, metavar="SECONDS",
                        help="fixed seconds between store page requests (default: a random "
                        "%g-%g s, resting %g-%g s every %d requests)"
                        % (CRAWL_GAP + CRAWL_REST + (CRAWL_REST_EVERY,)))
    parser.add_argument("--private-only", action="store_true",
                        help="with --fetch-stores and --cookie-file: refetch only the stores "
                        "cached as having no public listings, nothing else")
    parser.add_argument("--store-cache", default="store_cache.json",
                        help="where fetched store listings are kept (default: store_cache.json)")
    parser.add_argument("--stores", metavar="CSV",
                        help="write or refresh a CSV of unfiled brands to fill in with "
                        "Marketplace store URLs (your URLs are kept)")
    parser.add_argument("--self-test", action="store_true", help="run the built-in checks")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return
    if args.fetch_stores:
        cookie = None
        if args.cookie_file:
            with open(args.cookie_file, "r", encoding="utf-8") as f:
                cookie = " ".join(f.read().split()) or None
            if cookie and cookie.lower().startswith("cookie:"):
                cookie = cookie[7:].strip()
        try:
            if args.private_only and not cookie:
                parser.error("--private-only needs --cookie-file")
            fetch_stores(args.fetch_stores, args.store_cache, cookie=cookie, delay=args.delay,
                         private_only=args.private_only)
        except MarketplaceBlocked as e:
            sys.exit("Stopped: %s" % e)
        return
    if not args.path:
        parser.error("give the inventory cache file or folder (or --self-test)")
    path = args.path
    if os.path.isdir(path):
        path = icr.pick_cache_file(path)
    try:
        meta, categories, items = icr.read_cache(path)
        tri = Triage(Inventory(categories, items, getattr(meta, "creators", None)))
    except (OSError, ValueError, EOFError, icr.llsd.LLSDParseError) as exc:
        sys.exit("could not read %s: %s" % (path, exc))
    data = load_map(args.map)
    tri.use_store_cache(load_store_cache(args.store_cache))
    tri.apply_brand_names(data["brands"])
    tri.apply_decisions(data["decisions"])
    if args.list:
        list_groups(tri, data, args.list)
    elif args.plan:
        write_plan(tri, data, args.plan)
    elif args.stores:
        write_stores(tri, data, args.stores)
    else:
        try:
            interactive(tri, args.map, data)
        except (KeyboardInterrupt, EOFError):
            save_map(args.map, data)
            print("\nSaved. Run again to continue where you left off.")


if __name__ == "__main__":
    main()
