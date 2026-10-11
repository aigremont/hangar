#!/usr/bin/env python3
"""Merge a store cache fetched elsewhere (e.g. a public crawl run on another machine) into the
local one, without losing anything fetched here with a cookie.

    py merge_store_cache.py local.json incoming.json

For each store in the incoming file: a store the local file does not have is
copied in; a store the local file has keeps whichever copy holds more
listings (a signed-in fetch sees adult listings too, so it is never replaced
by a public one). Writes the local file atomically and prints what changed.
"""
import json
import os
import sys


def merge(local, incoming):
    added, replaced = [], []
    for key, entry in incoming.items():
        mine = local.get(key)
        if mine is None:
            local[key] = entry
            added.append(key)
        elif len(entry.get("listings", [])) > len(mine.get("listings", [])):
            local[key] = entry
            replaced.append(key)
    return added, replaced


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    local_path, incoming_path = sys.argv[1:]
    local = {}
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            local = json.load(f)
    with open(incoming_path, "r", encoding="utf-8") as f:
        incoming = json.load(f)
    added, replaced = merge(local, incoming)
    with open(local_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(local, f, ensure_ascii=False)
    os.replace(local_path + ".tmp", local_path)
    print("%s: %d stores (%d added, %d replaced by fuller copies)" % (
        local_path, len(local), len(added), len(replaced)))
    for key in added:
        print("  + " + key)
    for key in replaced:
        print("  ~ " + key)


if __name__ == "__main__":
    main()
