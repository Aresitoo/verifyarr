#!/usr/bin/env python3
"""Pinned tests for Verifyarr.

Pure logic, plus the paths that had NO coverage before the independent review: the CLI dispatch,
replace()'s gates, state retention, and the Telegram payload. Those are the tests that would have
caught the bugs that review found, so they matter as much as the predicate cases do.

Run either way:
    python3 tests/test_verifyarr.py      # prints a summary, exit 1 on failure
    pytest tests/test_verifyarr.py

These cases are the CONTRACT between this file's predicate and the one running in the author's own
doctor (server_watch.py, `_truth_signals`). If either side changes behaviour, a case here fails.

The fixtures in REAL_CASE and the candidate fixtures are not invented. They are the actual values
from a real incident: a file graded WEBDL-1080p whose media was 6-channel uncompressed PCM at 6.9
Mbps with a 2.6 Mbps video, a digital release 9.4 days out, flagged 2026-09-19. The replacement
offered later scored +864,080; the first thing an unfiltered version offered scored -139,399, which
is why is_approved_candidate exists.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import verifyarr as v  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("   %s" % detail if detail else ""))


def mi(audio="PCM", channels=6, audio_bps=6912000, video="x264", video_bps=2630605):
    return {"audioCodec": audio, "audioChannels": channels, "audioBitrate": audio_bps,
            "videoCodec": video, "videoBitrate": video_bps}


REAL_CASE = {"media": {"audioCodec": "PCM", "audioChannels": 6, "audioBitrate": 6912000,
                       "videoCodec": "x264", "videoBitrate": 2630605, "videoBitDepth": 8,
                       "resolution": "1920x804", "audioLanguages": "eng/spa", "subtitles": ""},
             "tier": "WEBDL-1080p", "digital_days": 9.4, "physical_days": 50.4}


# ---------------------------------------------------------------- the predicate: must flag
def test_real_incident_is_flagged():
    reasons = v.looks_like_disguised_preretail(**REAL_CASE)
    check("the real 2026-09-19 incident is flagged", len(reasons) >= 2, reasons)
    check("...and it names the uncompressed audio", any("uncompressed" in r for r in reasons))


def test_pcm_conjunction_is_flagged():
    check("PCM 6ch + WEBDL + digital 9 days out -> flagged",
          v.looks_like_disguised_preretail(mi(), "WEBDL-1080p", 9.4, 50.4) != [])


def test_bluray_uses_the_physical_date():
    check("PCM + BLURAY + physical 50 days out -> flagged",
          v.looks_like_disguised_preretail(mi(), "BLURAY-1080p", None, 50.4) != [])
    reasons = v.looks_like_disguised_preretail(mi(), "BLURAY-1080p", 10.0, 50.4)
    check("a disc claim reports the PHYSICAL date, not the digital one",
          any("physical release" in r for r in reasons) and
          not any("digital release" in r for r in reasons), reasons)


def test_inversion_alone_is_flagged():
    check("inversion alone (audio 2.6x a thin video) -> flagged",
          v.looks_like_disguised_preretail(mi(audio="EAC3"), "WEBDL-1080p", 9.4, 50.4) != [])


def test_candidate_accepts_a_hyphenated_tier_name():
    """The candidate side must normalise separators too, or a real upgrade looks like none exists."""
    check("WEB-DL-1080p candidate is accepted",
          v.is_approved_candidate({"title": "Movie.2026.1080p.WEB-DL.DDP5.1.H.264-GRP",
                                   "quality": {"quality": {"name": "WEB-DL-1080p"}},
                                   "approved": True, "downloadUrl": "http://idx/x.nzb",
                                   "guid": "abc123", "indexerId": 3}) is True)
    check("a candidate without a guid is still refused",
          v.is_approved_candidate({"title": "Movie.2026.1080p.WEB-DL.DDP5.1.H.264-GRP",
                                   "quality": {"quality": {"name": "WEBDL-1080p"}},
                                   "approved": True, "downloadUrl": "http://idx/x.nzb",
                                   "indexerId": 3}) is False)
    check("a candidate without a positive indexerId is still refused",
          v.is_approved_candidate({"title": "Movie.2026.1080p.WEB-DL.DDP5.1.H.264-GRP",
                                   "quality": {"quality": {"name": "WEBDL-1080p"}},
                                   "approved": True, "downloadUrl": "http://idx/x.nzb",
                                   "guid": "abc123", "indexerId": 0}) is False)


def test_tier_and_codec_variants_are_recognised():
    """Radarr's own names are WEBDL-1080p, but user-defined qualities and imported profiles vary."""
    check("WEB-DL-1080p (hyphenated) is recognised",
          v.looks_like_disguised_preretail(mi(), "WEB-DL-1080p", 9.4, 50.4) != [])
    check("web.dl spelling is recognised",
          v.looks_like_disguised_preretail(mi(), "web.dl", 9.4, 50.4) != [])
    check("codec pcm_s16le is recognised",
          v.looks_like_disguised_preretail(mi(audio="pcm_s16le"), "WEBDL-1080p", 9.4, 50.4) != [])
    check("codec 'Linear PCM' is recognised",
          v.looks_like_disguised_preretail(mi(audio="Linear PCM"), "WEBDL-1080p", 9.4, 50.4) != [])
    check("codec lpcm is still recognised",
          v.looks_like_disguised_preretail(mi(audio="lpcm"), "WEBDL-1080p", 9.4, 50.4) != [])


# ---------------------------------------------------------------- must NOT flag
def test_timeline_gate_protects_disc_rips():
    """The single most important negative case: a legitimate uncompressed-audio disc rip."""
    check("PCM + WEBDL but retail already out -> NOT flagged (timeline gate)",
          v.looks_like_disguised_preretail(mi(), "WEBDL-1080p", -30.5, -10.0) == [])
    check("PCM + BLURAY remux, retail long out -> NOT flagged (a legit disc rip)",
          v.looks_like_disguised_preretail(mi(), "BLURAY-1080p", -30.5, -10.0) == [])


def test_other_negatives():
    check("EAC3 (real stream audio) -> NOT flagged",
          v.looks_like_disguised_preretail(mi(audio="EAC3", audio_bps=640000),
                                           "WEBDL-1080p", 9.4, 50.4) == [])
    check("stereo PCM at a normal bitrate -> NOT flagged",
          v.looks_like_disguised_preretail(mi(channels=2, audio_bps=1536000, video_bps=6000000),
                                           "WEBDL-1080p", 9.4, 50.4) == [])
    check("HDTV tier + PCM -> NOT flagged (already graded correctly)",
          v.looks_like_disguised_preretail(mi(), "HDTV-1080p", 9.4, 50.4) == [])
    check("digital 2 days out (inside the margin) -> NOT flagged",
          v.looks_like_disguised_preretail(mi(), "WEBDL-1080p", 2.0, 50.4) == [])
    check("audio-heavy but video not thin -> NOT flagged",
          v.looks_like_disguised_preretail(mi(audio="EAC3", video_bps=6000000),
                                           "WEBDL-1080p", 9.4, 50.4) == [])
    check("no mediaInfo -> NOT flagged",
          v.looks_like_disguised_preretail(None, "WEBDL-1080p", 9.4, 50.4) == [])
    check("no tier -> NOT flagged",
          v.looks_like_disguised_preretail(mi(), "", 9.4, 50.4) == [])
    check("unknown retail dates -> NOT flagged (fail safe, never guess)",
          v.looks_like_disguised_preretail(mi(), "WEBDL-1080p", None, None) == [])


# ---------------------------------------------------------------- candidate and availability gates
def rel(title="Coyote.vs.Acme.2026.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-BYNDR",
        tier="WEBDL-1080p", approved=True, url="http://idx/x.nzb",
        guid="https://indexer/guid123", indexer_id=5):
    return {"title": title, "quality": {"quality": {"name": tier}}, "approved": approved,
            "downloadUrl": url, "customFormatScore": 864080,
            "guid": guid, "indexerId": indexer_id}


def test_candidate_gate():
    check("the real +864080 replacement is accepted", v.is_approved_candidate(rel()) is True)
    check("the -139399 near-tie that carded on 2026-09-29 is REJECTED",
          v.is_approved_candidate(rel(title="Coyote vs Acme 2026 1080p WEB-DL DDP 5 1 x265-GeneMige",
                                      approved=False)) is False)
    check("no approved flag -> rejected", v.is_approved_candidate(rel(approved=None)) is False)
    check("PCM in the title -> rejected",
          v.is_approved_candidate(rel(title="Movie.2026.1080p.WEB-DL.DCP.HEVC.PCM5.1-GRP")) is False)
    check("DCP in the title -> rejected",
          v.is_approved_candidate(rel(title="Movie.2026.Retail.DCP.WEBRip.x265.DDP5.1-ADD")) is False)
    check("CAM in the title -> rejected",
          v.is_approved_candidate(rel(title="Movie.2026.1080p.CAM.x264-DKS")) is False)
    check("HDTV tier -> rejected", v.is_approved_candidate(rel(tier="HDTV-1080p")) is False)
    check("no downloadUrl -> rejected", v.is_approved_candidate(rel(url=None)) is False)
    check("empty dict -> rejected", v.is_approved_candidate({}) is False)
    check("no guid -> rejected", v.is_approved_candidate(rel(guid=None)) is False)
    check("empty guid -> rejected", v.is_approved_candidate(rel(guid="")) is False)
    check("no indexerId -> rejected", v.is_approved_candidate(rel(indexer_id=None)) is False)
    check("zero indexerId -> rejected", v.is_approved_candidate(rel(indexer_id=0)) is False)


def test_availability_gate():
    check("available + monitored -> grabbable",
          v.movie_is_grabbable({"isAvailable": True, "monitored": True}) is True)
    check("isAvailable False -> refused",
          v.movie_is_grabbable({"isAvailable": False, "monitored": True}) is False)
    check("UNMONITORED -> refused (Radarr would never grab, so deleting loses the file)",
          v.movie_is_grabbable({"isAvailable": True, "monitored": False}) is False)
    check("missing monitored -> refused (fail safe)",
          v.movie_is_grabbable({"isAvailable": True}) is False)
    check("missing isAvailable -> refused",
          v.movie_is_grabbable({"monitored": True}) is False)
    check("None -> refused", v.movie_is_grabbable(None) is False)


def test_days_until():
    check("a real date parses", v.days_until("2026-09-29", now=1759017600) is not None)
    check("a missing date is None", v.days_until("") is None)
    check("junk is None", v.days_until("not-a-date") is None)
    check("None is None", v.days_until(None) is None)


def test_retention_helper():
    now = time.time()
    recent = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 3600))
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 40 * 86400))
    check("a fresh tombstone is retained", v._within_days(recent, 30, now) is True)
    check("a 40-day-old tombstone expires", v._within_days(old, 30, now) is False)
    check("a malformed stamp expires rather than living forever",
          v._within_days("nonsense", 30, now) is False)
    check("a missing stamp expires", v._within_days(None, 30, now) is False)


# ---------------------------------------------------------------- the paths that had no coverage
class FakeRadarr:
    """Stands in for the client so scan(), replace() and main() can be exercised offline."""

    def __init__(self, movies, releases=None):
        self._movies = movies
        self._releases = releases or {}
        self.deleted, self.searched, self.tagged, self.grabbed = [], [], [], []

    def movies(self):
        return self._movies

    def get(self, path):
        mid = str(path).split("/")[-1]
        for m in self._movies:
            if str(m["id"]) == mid:
                return m
        raise KeyError(mid)

    def releases(self, mid):
        return self._releases.get(str(mid), [])

    def ensure_tag(self, label):
        return 7

    def add_tag(self, movie, tag_id):
        self.tagged.append(movie["id"])
        return True

    def remove_tag(self, movie, tag_id):
        self.removed = getattr(self, "removed", [])
        self.removed.append((movie.get("id"), tag_id))
        return True

    # Kept deliberately even though the real client no longer deletes anything: if a
    # delete path is ever reintroduced, this records it and the deleted == [] assertions
    # below fail.
    def delete_file(self, file_id):
        self.deleted.append(file_id)

    def search(self, mid):
        self.searched.append(mid)

    def grab(self, guid, indexer_id):
        self.grabbed.append({"guid": guid, "indexerId": indexer_id})


def _date(days):
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + days * 86400))


def a_movie(mid=61, monitored=True, available=True, with_file=True, tier="WEBDL-1080p"):
    m = {"id": mid, "title": "WALL_E", "hasFile": with_file, "isAvailable": available,
         "monitored": monitored, "status": "inCinemas", "qualityProfileId": 9,
         "digitalRelease": _date(9), "physicalRelease": _date(50)}
    if with_file:
        m["movieFile"] = {"id": 40, "relativePath": "X.mkv", "mediaInfo": REAL_CASE["media"],
                          "quality": {"quality": {"name": tier}}, "releaseGroup": "DKS"}
    return m


def _cfg(tmp, mode="report", state=None):
    return {"radarr": {"url": "http://x", "api_key": "k"}, "mode": mode,
            "state_file": state or os.path.join(tmp, "state.json"),
            "tag": {"enabled": False}, "telegram": {"enabled": False},
            "predicate": {}}


def test_scan_flags_and_skips_a_no_op_rewrite():
    tmp = tempfile.mkdtemp()
    client = FakeRadarr([a_movie()])
    cfg = _cfg(tmp)
    calls, real = [], v.save_state
    v.save_state = lambda p, s: (calls.append(p), real(p, s))[1]
    try:
        findings, state = v.scan(cfg, client)
        check("scan flags the real-case file", len(findings) == 1 and len(state) == 1)
        first = len(calls)
        v.scan(cfg, client)
        check("a second scan with no change does NOT rewrite the state file",
              len(calls) == first, "save_state calls: %d then %d" % (first, len(calls)))
    finally:
        v.save_state = real


def test_scan_keeps_a_fresh_tombstone_and_evicts_the_rest():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 40 * 86400))
    with open(cfg["state_file"], "w") as f:
        json.dump({"61:40": {"movieId": 61, "fileId": 40, "status": "replace_requested",
                             "replacedAt": now, "title": "fresh"},
                   "62:41": {"movieId": 62, "fileId": 41, "status": "replace_requested",
                             "replacedAt": old, "title": "stale"},
                   "63:42": {"movieId": 63, "fileId": 42, "status": "awaiting_retail",
                             "title": "ordinary"}}, f)
    client = FakeRadarr([a_movie(64, with_file=False)])   # none of those three has a file now
    findings, state = v.scan(cfg, client)
    check("a fresh replacement record is retained (no amnesia)", "61:40" in state, sorted(state))
    check("a 40-day-old record expires", "62:41" not in state, sorted(state))
    check("an ordinary flag for a gone file is evicted", "63:42" not in state, sorted(state))


def test_replace_refuses_every_failing_gate():
    cases = [
        ("unmonitored", a_movie(monitored=False), [rel()]),
        ("unavailable", a_movie(available=False), [rel()]),
        ("no approved candidate", a_movie(), [rel(approved=False)]),
        ("no candidates at all", a_movie(), []),
        ("the file changed since it was flagged", a_movie(), [rel()]),
    ]
    for label, movie, releases in cases:
        tmp = tempfile.mkdtemp()
        cfg = _cfg(tmp, mode="replace")
        want = 40 if label != "the file changed since it was flagged" else 999
        with open(cfg["state_file"], "w") as f:
            json.dump({"61:%d" % want: {"movieId": 61, "fileId": want,
                                        "status": "awaiting_retail", "title": "WALL_E"}}, f)
        client = FakeRadarr([movie], {"61": releases})
        v.replace(cfg, client, confirm=True)
        check("replace refuses when %s" % label, client.deleted == [] and client.grabbed == [],
              "deleted=%s grabbed=%s" % (client.deleted, client.grabbed))


def test_replace_grabs_when_all_gates_pass():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp, mode="replace")
    with open(cfg["state_file"], "w") as f:
        json.dump({"61:40": {"movieId": 61, "fileId": 40, "status": "awaiting_retail",
                             "title": "WALL_E"}}, f)
    client = FakeRadarr([a_movie()], {"61": [rel()]})
    done = v.replace(cfg, client, confirm=True)
    check("replace grabs without deleting the incumbent file",
          client.deleted == [] and client.grabbed == [{"guid": "https://indexer/guid123", "indexerId": 5}] and done == 1,
          "deleted=%s grabbed=%s" % (client.deleted, client.grabbed))
    state = json.load(open(cfg["state_file"]))
    check("the record is marked replace_requested",
          state["61:40"]["status"] == "replace_requested", state.get("61:40", {}).get("status"))
    check("the record retains grabbed guid and indexerId",
          state["61:40"].get("grabbedGuid") == "https://indexer/guid123" and
          state["61:40"].get("grabbedIndexerId") == 5)


def test_cli_replace_command_actually_replaces():
    """The dispatch bug the review found. Before the fix, this command scanned and did nothing."""
    tmp = tempfile.mkdtemp()
    cfg_path, state_path = os.path.join(tmp, "config.json"), os.path.join(tmp, "state.json")
    fake = FakeRadarr([a_movie()], {"61": [rel()]})
    with open(state_path, "w") as f:
        json.dump({"61:40": {"movieId": 61, "fileId": 40, "status": "awaiting_retail",
                             "title": "WALL_E"}}, f)
    with open(cfg_path, "w") as f:                      # mode is deliberately the DEFAULT
        json.dump({"radarr": {"url": "http://x", "api_key": "k"}, "mode": "report",
                   "state_file": state_path, "tag": {"enabled": False},
                   "telegram": {"enabled": False}, "predicate": {}}, f)
    real_client = v.Radarr
    v.Radarr = lambda *a, **k: fake
    try:
        rc = v.main(["replace", "--config", cfg_path, "--yes"])
        check("`verifyarr replace` acts even when the config says mode: report",
              fake.deleted == [] and fake.grabbed == [{"guid": "https://indexer/guid123", "indexerId": 5}],
              "deleted=%s grabbed=%s rc=%s" % (fake.deleted, fake.grabbed, rc))
        check("a completed replace exits 0 (nothing outstanding)", rc == 0, "rc=%s" % rc)
    finally:
        v.Radarr = real_client


def test_scan_command_exit_code():
    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.json")
    fake = FakeRadarr([a_movie()])
    with open(cfg_path, "w") as f:
        json.dump({"radarr": {"url": "http://x", "api_key": "k"}, "mode": "report",
                   "state_file": os.path.join(tmp, "s.json"), "tag": {"enabled": False},
                   "telegram": {"enabled": False}, "predicate": {}}, f)
    real_client = v.Radarr
    v.Radarr = lambda *a, **k: fake
    try:
        check("`scan` exits 1 when something is flagged",
              v.main(["scan", "--config", cfg_path]) == 1)
    finally:
        v.Radarr = real_client


def test_telegram_payload_is_escaped_html():
    sent = {}

    class FakeResp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = v.urllib.request.urlopen
    v.urllib.request.urlopen = lambda req, timeout=None: (sent.update(
        json.loads(req.data.decode())), FakeResp())[1]
    try:
        cfg = {"timeout": 5, "telegram": {"enabled": True, "token": "t", "chat_id": "1"},
               "radarr": {"url": "http://x", "api_key": "k"}}
        v.notify_telegram(cfg, "Verifyarr: Fast & Furious [REC] WALL_E")
        check("telegram uses HTML, not Markdown", sent.get("parse_mode") == "HTML",
              sent.get("parse_mode"))
        check("the ampersand is escaped, so the API will not 400",
              "&amp;" in (sent.get("text") or ""), sent.get("text"))
        check("underscores and brackets survive as plain text",
              "WALL_E" in (sent.get("text") or "") and "[REC]" in (sent.get("text") or ""))
    finally:
        v.urllib.request.urlopen = real


def test_a_cleared_flag_lifts_the_tag():
    """The tag must reflect the current verdict, not the history of one old run."""
    tmp = tempfile.mkdtemp()
    cfg = _cfg(tmp)
    cfg["tag"] = {"enabled": True}
    with open(cfg["state_file"], "w") as f:
        json.dump({"61:40": {"movieId": 61, "fileId": 40, "status": "awaiting_retail",
                             "title": "WALL_E"}}, f)
    clean = a_movie()                      # EAC3 stream audio: nothing left to flag
    clean["movieFile"]["mediaInfo"] = mi(audio="EAC3", audio_bps=640000)
    client = FakeRadarr([clean])
    v.scan(cfg, client)
    check("a movie whose flag cleared has its tag removed",
          getattr(client, "removed", []) == [(61, 7)],
          "removed=%s" % getattr(client, "removed", None))
    check("and it is dropped from the state", not json.load(open(cfg["state_file"])),
          json.load(open(cfg["state_file"])))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    bad = [n for n, ok in RESULTS if not ok]
    print("\nVERIFYARR TESTS: %d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
