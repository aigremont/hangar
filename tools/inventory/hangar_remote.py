#!/usr/bin/env python3
"""Drive the Hangar plugin in a running Veles over its local control channel.

    python hangar_remote.py check
    python hangar_remote.py fetch            # polls /lines until the step finishes
    python hangar_remote.py apply 5
    python hangar_remote.py --status

The user must have typed `//hangar remote on` in Veles first; that writes
%LOCALAPPDATA%\\Hangar\\remote.json (port, token, avatar). Stdlib only.

Exit codes: 0 done, 1 the command said it could not / failed, 2 transport or
HTTP error (including 401/403), 3 timed out.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

POLL_SECONDS = 2
TIMEOUT_SECONDS = 30 * 60
# Lines that end a started command (the plugin's own wording).
FINISH = re.compile(r"fetched |apply finished|verified:|undo finished|exported |could not|failed")
# A line that reports an error ("fetch failed: ...", "could not ..."), not the "failed 0" in a summary.
FAIL = re.compile(r"could not|\w failed:|^STOPPED:")


def token_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Hangar")


def token_files():
    """All remote-*.json files as (path, config), sorted by file name."""
    found = []
    for path in sorted(glob.glob(os.path.join(glob.escape(token_dir()), "remote-*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["port"], cfg["token"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found.append((path, cfg))
    return found


def slug(name):
    return re.sub(r"[^A-Za-z0-9]", "_", name or "")


def pick(files, avatar):
    """The (path, config) to use, or an error message string."""
    if avatar:
        want = avatar.lower()
        want_slug = slug(avatar).lower()
        hits = [(p, c) for p, c in files
                if want in str(c.get("avatar", "")).lower()
                or want_slug in slug(str(c.get("avatar", ""))).lower()
                or want_slug in os.path.basename(p).lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            return "no Hangar remote matches '%s'; on: %s" % (avatar, describe(files))
        return "'%s' matches several avatars: %s" % (avatar, describe(hits))
    if len(files) == 1:
        return files[0]
    if not files:
        return "no Hangar remote is on (type //hangar remote on in Veles first)"
    return "several Hangar remotes are on; pick one with --avatar: %s" % describe(files)


def describe(files):
    return ", ".join(str(c.get("avatar") or os.path.basename(p)) for p, c in files)


def load_config(avatar=None):
    chosen = pick(token_files(), avatar)
    if isinstance(chosen, str):
        print(chosen, file=sys.stderr)
        sys.exit(2)
    return chosen[1]


class Remote:
    def __init__(self, cfg):
        self.base = "http://127.0.0.1:%d" % cfg["port"]
        self.token = cfg["token"]

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            print("HTTP %d %s" % (e.code, detail), file=sys.stderr)
            sys.exit(2)
        except (urllib.error.URLError, OSError) as e:
            sys.exit("cannot reach Hangar (is remote on, and is Veles running?): %s" % e)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    avatar = None
    if "--avatar" in argv:
        i = argv.index("--avatar")
        if i + 1 >= len(argv):
            print("--avatar needs a name", file=sys.stderr)
            return 2
        avatar = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if argv == ["--list"]:
        files = token_files()
        if not files:
            print("no Hangar remote is on")
        for _, c in files:
            print("%s	port %s	pid %s" % (c.get("avatar", "?"), c["port"], c.get("pid", "?")))
        return 0
    if argv == ["--status"] and not avatar and not token_files():
        print("no Hangar remote is on")
        return 0
    if not argv:
        print("no command given", file=sys.stderr)
        return 2
    remote = Remote(load_config(avatar))
    if argv == ["--status"]:
        print(json.dumps(remote.call("GET", "/status"), indent=2))
        return 0

    text = " ".join(argv)
    reply = remote.call("POST", "/command", {"text": text})
    failed = False
    for line in reply.get("lines", []):
        print(line)
        failed = failed or bool(FAIL.search(line))
    if not reply.get("started"):
        return 1 if failed else 0

    since = reply.get("next", 0)
    deadline = time.time() + TIMEOUT_SECONDS
    marker_seen = False
    grace = 0
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        r = remote.call("GET", "/lines?since=%d" % since)
        for item in r.get("lines", []):
            print(item["text"], flush=True)
            if FINISH.search(item["text"]):
                marker_seen = True
                failed = failed or bool(FAIL.search(item["text"]))
            since = item["seq"]
        since = max(since, r.get("next", since))
        if r.get("truncated"):
            print("(some lines were lost: the buffer holds the last 2000)", file=sys.stderr)
        # The plugin saying it is no longer busy ends the wait (some endings, such
        # as a partial verify, have no marker). After a marker, give the step up to
        # 10 more seconds to finish so trailing warnings are printed too.
        if not r.get("busy", False):
            return 1 if failed else 0
        if marker_seen:
            grace += 1
            if grace >= 5:
                return 1 if failed else 0
    print("timed out after 30 minutes; the step may still be running (--status)", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
