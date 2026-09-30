#!/usr/bin/env python3
"""Verifyarr - does the file you actually have match the quality it was graded as?

WHY THIS EXISTS, IN ONE PARAGRAPH
Custom formats in Radarr have no conditions for media properties. They match on the release title,
source, resolution, size, group and a few others, never on what the file actually contains. A Radarr
maintainer confirmed this in public on issue #11730: "it doesn't have specific conditions for
mediainfo". So a download can be graded WEBDL-1080p, satisfy your cutoff, and turn out to be an
uncompressed-audio capture. Nothing in the stack can see that. This can: Radarr already stores what
it found in movieFile.mediaInfo, and nobody reads it.

AND WHY IT MATTERS MORE THAN IT SOUNDS
Once a file satisfies the cutoff, Radarr stops periodically searching that movie. The cut-off unmet
list is what drives scheduled upgrade searches, and a movie that meets its cutoff is not on it. So a
mislabelled file is not merely wrong, it is quiet: it sits there, and the only thing that will ever
replace it is RSS happening to catch a better release while it is still fresh in the feed. That is
the window `mode: replace` exists to close.

DEFAULT BEHAVIOUR IS REPORT ONLY. Read the README's SCOPE and KNOWN LIMITS sections before changing
`mode` to `replace`. The predicate in this file is a twin of the one running in the author's own
doctor; tests/test_verifyarr.py is the contract that keeps the two answering identically.
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"

RETAIL_TIERS = ("WEBDL", "WEBRIP", "BLURAY", "REMUX")
PCM_CODECS = ("pcm", "lpcm")
DEFAULT_TAG = "verifyarr-suspect"

# Defaults for the predicate. Changing these changes what gets flagged; the README explains which
# one is the load-bearing one (min_days_to_retail) and what happens if you weaken it.
TOMBSTONE_DAYS = 30      # how long a replacement record outlives the file it deleted

DEFAULTS = {
    "min_days_to_retail": 3,      # a retail drop inside this window is not treated as a leak
    "thin_video_bps": 3_500_000,  # "thin video" bound for the audio-inversion signal
    "audio_ratio": 1.5,           # audio bitrate versus video bitrate
    "pcm_min_channels": 5,        # uncompressed audio at this many channels or more
}


# --------------------------------------------------------------------------- pure predicate

def days_until(value, now=None):
    """Whole days from now until an ISO date, or None if there is no usable date. Pure."""
    s = str(value or "")[:10]
    if len(s) != 10:
        return None
    try:
        # timegm, not mktime: arr dates are UTC, and mktime would read them as local time, shifting
        # every answer by the timezone offset. Same lesson as the doctor's own history parser.
        when = calendar.timegm(time.strptime(s, "%Y-%m-%d"))
    except Exception:
        return None
    return (when - (now if now is not None else time.time())) / 86400.0


def _within_days(stamp, days, now=None):
    """PURE. Was a 'YYYY-MM-DD HH:MM:SS' stamp recorded within the last N days?

    Unknown or unparseable means False, so a malformed record expires rather than living forever.
    """
    try:
        when = calendar.timegm(time.strptime(str(stamp), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    return ((now if now is not None else time.time()) - when) < days * 86400


def looks_like_disguised_preretail(media, tier, digital_days, physical_days, **kw):
    """PURE. Returns a list of reasons, or an empty list.

    Three independent things have to agree:
      1. the arr graded the file a retail tier, so the grader already believes it is retail grade;
      2. the media contradicts that grade: uncompressed 5.1+ PCM (streams deliver DD+/AAC/EAC3,
         never raw PCM), or audio dwarfing a thin video;
      3. the retail date says the file could not legitimately exist yet.

    Gate 3 is not optional and is not decoration. Without it, a legitimate uncompressed-audio disc
    rip (LPCM on older Blurays and concert discs) is indistinguishable from a capture, so the tool
    would start accusing people's own discs. Gate 1 keeps HDTV and CAM rips out, because those are
    already graded correctly and need no help.
    """
    p = dict(DEFAULTS, **(kw or {}))
    # Strip separators before matching. Radarr's own names are WEBDL-1080p, but user-defined
    # qualities and imported profiles turn up as WEB-DL-1080p, web.dl, WEB_DL and similar, and a
    # strict startswith would silently skip those files rather than judge them.
    t = re.sub(r"[\s._-]", "", str(tier or "").upper())
    mi = media or {}
    if not mi or not t.startswith(RETAIL_TIERS):
        return []
    codec = str(mi.get("audioCodec") or "").lower()
    channels = mi.get("audioChannels") or 0
    audio_bps = mi.get("audioBitrate") or 0
    video_bps = mi.get("videoBitrate") or 0

    # Substring, not equality: ffprobe-derived names arrive as pcm_s16le, PCM 24-bit, Linear PCM.
    line_audio = any(c in codec for c in PCM_CODECS) and channels >= p["pcm_min_channels"]
    inverted = bool(audio_bps and video_bps
                    and audio_bps > video_bps * p["audio_ratio"]
                    and video_bps < p["thin_video_bps"])
    if not (line_audio or inverted):
        return []

    if t.startswith(("BLURAY", "REMUX")):
        allowed = physical_days is not None and physical_days > p["min_days_to_retail"]
    else:
        allowed = digital_days is not None and digital_days > p["min_days_to_retail"]
    if not allowed:
        return []

    reasons = []
    if line_audio:
        reasons.append("uncompressed %s %s-channel audio (%.1f Mbps), which is a disc or "
                       "line-capture signature rather than a stream"
                       % (mi.get("audioCodec"), channels, audio_bps / 1e6))
    if inverted:
        reasons.append("audio bitrate is %.1fx the video bitrate (video only %.1f Mbps)"
                       % (audio_bps / float(video_bps), video_bps / 1e6))
    # Report the date the gate judged. A disc tier was allowed through on the PHYSICAL date, so
    # quoting the digital one there would be true of the movie and wrong about the reason.
    if t.startswith(("BLURAY", "REMUX")):
        reasons.append("the physical release is still %.1f days away" % physical_days)
    elif digital_days is not None and digital_days > p["min_days_to_retail"]:
        reasons.append("the digital release is still %.1f days away" % digital_days)
    return reasons


def is_approved_candidate(rel):
    """PURE. Is this release fit to replace the file with?

    `approved` is the arr's own verdict, so this can never offer something the profile would refuse.
    Verified the hard way: an unfiltered version of this offered a release scoring -139,399 as
    "genuine retail" against an incumbent at -139,999, i.e. no upgrade at all.
    """
    if not str(rel.get("title") or "") or not rel.get("downloadUrl"):
        return False
    if re.search(r"\b(pcm|lpcm|line|ts|cam|dcp)\b", str(rel["title"]), re.I):
        return False
    quality = ((rel.get("quality") or {}).get("quality") or {}).get("name") or ""
    if not quality.upper().startswith(RETAIL_TIERS):
        return False
    return rel.get("approved") is True


def movie_is_grabbable(movie):
    """PURE. Would a search actually be able to grab anything for this movie?

    `isAvailable` is computed by the arr from minimumAvailability plus the release dates, so it is
    the field to trust rather than the raw status string. Missing or false means refuse: deleting a
    file first while nothing can replace it is the one outcome worth avoiding.
    """
    m = movie or {}
    if m.get("isAvailable") is not True:
        return False
    # Radarr will not grab for an unmonitored movie, so deleting its file first would leave nothing
    # that can replace it. Refusing costs nothing and removes the one-way door.
    return m.get("monitored") is True


# --------------------------------------------------------------------------- config and client

def _die(msg, code=2):
    """Config and API problems exit 2, so they can never be mistaken for the exit-1 findings case."""
    print("verifyarr: %s" % msg, file=sys.stderr)
    raise SystemExit(code)


def load_config(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        _die("no config at %s (copy config.example.json and fill it in)" % path)
    except Exception as e:
        _die("config %s is not readable JSON: %s" % (path, str(e)[:80]))
    rad = cfg.get("radarr") or {}
    if not rad.get("url") or not rad.get("api_key"):
        _die("config %s needs radarr.url and radarr.api_key" % path)
    cfg.setdefault("mode", "report")
    if cfg["mode"] not in ("report", "replace"):
        _die("mode must be report or replace, not %r" % cfg["mode"])
    cfg.setdefault("state_file", "verifyarr-state.json")
    cfg.setdefault("timeout", 30)
    return cfg


class Radarr:
    """Minimal client. Uses the ?apikey= query form because that is the form in use and verified."""

    def __init__(self, url, api_key, timeout=30):
        self.base = url.rstrip("/") + "/api/v3"
        self.key = api_key
        self.timeout = timeout

    def _url(self, path):
        return "%s/%s%sapikey=%s" % (self.base, path, "&" if "?" in path else "?", self.key)

    def call(self, method, path, body=None):
        req = urllib.request.Request(self._url(path), method=method)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def get(self, path):
        return self.call("GET", path)

    def movies(self):
        return self.get("movie") or []

    def releases(self, movie_id):
        return self.get("release?movieId=%s" % movie_id) or []

    def ensure_tag(self, label):
        """Idempotent: an existing label comes back with its original id rather than a duplicate."""
        for t in (self.get("tag") or []):
            if t.get("label") == label:
                return t["id"]
        made = self.call("POST", "tag", {"label": label})
        return (made or {}).get("id")

    def add_tag(self, movie, tag_id):
        """Union, never replace: read the movie's tags and write them back plus ours."""
        current = list(movie.get("tags") or [])
        if tag_id in current:
            return False
        self.call("PUT", "movie/editor", {"movieIds": [movie["id"]], "tags": current + [tag_id]})
        return True

    def remove_tag(self, movie, tag_id):
        current = list(movie.get("tags") or [])
        if tag_id not in current:
            return False
        self.call("PUT", "movie/editor",
                  {"movieIds": [movie["id"]], "tags": [t for t in current if t != tag_id]})
        return True

    def delete_file(self, file_id):
        return self.call("DELETE", "moviefile/%s" % file_id)

    def search(self, movie_id):
        return self.call("POST", "command", {"name": "MoviesSearch", "movieIds": [int(movie_id)]})


def notify_telegram(cfg, text):
    t = cfg.get("telegram") or {}
    if not t.get("enabled") or not t.get("token") or not t.get("chat_id"):
        return False
    # escape() + HTML: Markdown V1 rejects a message containing _ * or [ unescaped, which is most
    # movie titles that matter (WALL_E, [REC], Mission_Impossible), and the API then drops the
    # notification with a 400 that a cron user never sees.
    data = {"chat_id": t["chat_id"], "text": html.escape(text), "parse_mode": "HTML"}
    if t.get("thread_id"):
        data["message_thread_id"] = int(t["thread_id"])
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % t["token"],
                                 data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 30)) as r:
            return r.status == 200
    except Exception as e:
        print("telegram failed: %s" % str(e)[:80], file=sys.stderr)
        return False


# --------------------------------------------------------------------------- state

def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("state unreadable, starting fresh: %s" % str(e)[:80], file=sys.stderr)
        return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- commands

def scan(cfg, client, now=None, dry=False):
    """Report mode. Detects, optionally tags, optionally notifies. Touches no file ever."""
    state = load_state(cfg["state_file"])
    state_before = json.dumps(state, sort_keys=True)
    pred = cfg.get("predicate") or {}
    tag_cfg = cfg.get("tag") or {}
    tag_id = None
    if tag_cfg.get("enabled") and not dry:
        tag_id = client.ensure_tag(tag_cfg.get("label") or DEFAULT_TAG)

    findings = []
    seen = set()
    for movie in client.movies():
        mf = movie.get("movieFile") or {}
        if not movie.get("hasFile") or not mf:
            continue
        key = "%s:%s" % (movie.get("id"), mf.get("id"))
        seen.add(key)
        reasons = looks_like_disguised_preretail(
            mf.get("mediaInfo"),
            ((mf.get("quality") or {}).get("quality") or {}).get("name"),
            days_until(movie.get("digitalRelease"), now),
            days_until(movie.get("physicalRelease"), now),
            **pred)
        if not reasons:
            continue
        entry = {
            "movieId": movie.get("id"), "fileId": mf.get("id"), "title": movie.get("title"),
            "claimedQuality": ((mf.get("quality") or {}).get("quality") or {}).get("name"),
            "releaseGroup": mf.get("releaseGroup"),
            "digitalRelease": str(movie.get("digitalRelease") or "")[:10],
            "physicalRelease": str(movie.get("physicalRelease") or "")[:10],
            "reasons": reasons,
            "flaggedAt": (state.get(key) or {}).get("flaggedAt") or time.strftime("%Y-%m-%d"),
            "status": (state.get(key) or {}).get("status") or "awaiting_retail",
        }
        fresh = key not in state
        state[key] = entry
        findings.append((movie, mf, entry, fresh))

    # A file that is gone, or replaced by a new fileId, no longer needs watching, EXCEPT a key whose
    # replacement we already started. Evicting that immediately would make us forget that we deleted
    # a file and why, which is how you end up with an empty library and no record of the cause.
    for key in [k for k in state if k not in seen]:
        entry = state[key]
        if entry.get("status") == "replace_requested" and \
                _within_days(entry.get("replacedAt"), TOMBSTONE_DAYS):
            continue
        state.pop(key, None)
    if tag_cfg.get("enabled") and tag_id and not dry:
        for movie, mf, entry, fresh in findings:
            if client.add_tag(movie, tag_id):
                print("tagged: %s" % entry["title"])
    if not dry and json.dumps(state, sort_keys=True) != state_before:
        save_state(cfg["state_file"], state)

    for movie, mf, entry, fresh in findings:
        print("%s%s [%s]  graded %s" % ("NEW  " if fresh else "     ", entry["title"],
                                        mf.get("relativePath") or "", entry["claimedQuality"]))
        for r in entry["reasons"]:
            print("        - %s" % r)
        if fresh:
            card = ("Verifyarr: %s is graded %s but the media says otherwise:\n%s"
                    % (entry["title"], entry["claimedQuality"],
                       "\n".join("  - %s" % r for r in entry["reasons"])))
            if cfg.get("mode") == "replace":
                card += ("\n\n`mode: replace` is on, so the next run will delete this file and "
                         "search again, but only if the arr has accepted a replacement and the "
                         "movie is grabbable.")
            notify_telegram(cfg, card)
    return findings, state


def replace(cfg, client, confirm=False, now=None, dry=False):
    """The opt-in half. Three gates, all re-checked immediately before anything is deleted."""
    if cfg.get("mode") != "replace":
        print("replace mode is off (mode: %s). Nothing done." % cfg.get("mode"))
        return 0
    state = load_state(cfg["state_file"])
    state_before = json.dumps(state, sort_keys=True)
    done = 0
    for key, entry in list(state.items()):
        if entry.get("status") not in ("awaiting_retail", None):
            continue
        movie_id = entry.get("movieId")
        try:
            movie = client.get("movie/%s" % movie_id)
        except Exception as e:
            print("skip %s: movie unreadable (%s)" % (key, str(e)[:50]))
            continue

        # gate 1: the file on disk is still the flagged file
        mf = movie.get("movieFile") or {}
        if not mf or str(mf.get("id")) != str(entry.get("fileId")):
            print("skip %s: the file changed since it was flagged" % key)
            continue

        # gate 2: the arr says the movie is grabbable
        if not movie_is_grabbable(movie):
            print("skip %s: arr reports the movie as unavailable (%s)"
                  % (key, movie.get("status")))
            continue

        # gate 3: something the arr itself accepts is on offer
        try:
            approved = [r for r in client.releases(movie_id) if is_approved_candidate(r)]
        except Exception as e:
            print("skip %s: candidate search failed (%s)" % (key, str(e)[:50]))
            continue
        if not approved:
            print("skip %s: no approved replacement on offer yet" % key)
            continue

        best = max(approved, key=lambda r: r.get("customFormatScore") or -10 ** 9)
        print("READY %s: %s" % (entry.get("title"), str(best.get("title"))[:70]))
        print("      deletes file id %s, then searches (%d approved option(s), best score %s)"
              % (mf.get("id"), len(approved), best.get("customFormatScore")))
        if dry:
            continue
        if not confirm:
            try:
                if input("      proceed? [y/N] ").strip().lower() != "y":
                    print("      skipped")
                    continue
            except EOFError:
                print("      no tty and no --yes, skipping")
                continue
        client.delete_file(mf.get("id"))
        client.search(movie_id)
        entry["status"] = "replace_requested"
        entry["replacedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["candidate"] = str(best.get("title"))[:120]
        entry["candidateScore"] = best.get("customFormatScore")
        done += 1
        notify_telegram(cfg, "Verifyarr: deleted %s and searched again, best approved option "
                             "is the one above." % entry.get("title"))
    if json.dumps(state, sort_keys=True) != state_before:
        save_state(cfg["state_file"], state)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verifyarr",
                                 description="Check whether files match the quality they were "
                                             "graded as. Report only unless mode is replace.")
    ap.add_argument("command", choices=("scan", "replace", "doctor"), nargs="?",
                    help="scan (default), replace, or doctor. Falls back to the config's mode.")
    ap.add_argument("--config", default=os.environ.get("VERIFYARR_CONFIG", "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("--yes", action="store_true", help="do not prompt before deleting")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    ap.add_argument("--version", action="version", version="verifyarr %s" % __version__)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    client = Radarr((cfg["radarr"])["url"], (cfg["radarr"])["api_key"], cfg.get("timeout", 30))

    if args.command == "doctor":
        print("config  : %s" % args.config)
        print("radarr  : %s" % (cfg["radarr"])["url"])
        print("mode    : %s" % cfg.get("mode"))
        print("tag     : %s" % (cfg.get("tag") or {}))
        print("telegram: %s" % ("enabled" if (cfg.get("telegram") or {}).get("enabled")
                                else "disabled"))
        try:
            print("movies  : %d visible to the API" % len(client.movies()))
        except Exception as e:
            print("movies  : API call failed: %s" % str(e)[:80])
        return 0

    # The positional command decides what runs; the config value is only the default for a bare
    # invocation. Previously the argument was parsed and then ignored, so `verifyarr replace` scanned
    # and exited while the README said otherwise.
    command = args.command or ("replace" if cfg.get("mode") == "replace" else "scan")
    if args.command == "replace" and cfg.get("mode") != "replace":
        # The explicit command overrides the config default, which is what the README documents. The
        # prompt, or --yes, is still what gates the deletion, so this cannot delete anything by
        # surprise: the config value only ever decided what a BARE invocation does.
        print("note: replace requested on the command line; the config says mode: %s"
              % cfg.get("mode"))
        cfg["mode"] = "replace"

    try:
        findings, state = scan(cfg, client, dry=args.dry_run)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        _die("Radarr call failed: %s" % str(e)[:120])
    if args.json:
        print(json.dumps([e for _, _, e, _ in findings], indent=2))

    if command == "scan":
        if not findings:
            print("no flagged files. %d entr(y/ies) in state." % len(state))
            return 0
        print("%d flagged file(s)." % len(findings))
        return 1

    replace(cfg, client, confirm=args.yes, dry=args.dry_run)
    # In replace mode the exit code answers "is anything still outstanding", not "was anything
    # flagged", so a cron job can tell a completed run from one that still needs attention.
    outstanding = [e for e in load_state(cfg["state_file"]).values()
                   if e.get("status") == "awaiting_retail"]
    if not outstanding:
        print("nothing outstanding.")
        return 0
    print("%d item(s) still awaiting retail." % len(outstanding))
    return 1


if __name__ == "__main__":
    sys.exit(main())
