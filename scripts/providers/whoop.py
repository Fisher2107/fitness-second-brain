"""WHOOP provider (official WHOOP Developer API, v2).

Pulls workouts, sleep, recovery and day strain from the WHOOP cloud. WHOOP is
strongest on recovery data (HRV, resting HR, sleep, recovery %, strain); it
has no VO2max or pace data, and a workout only carries distance when it was
recorded with GPS in the WHOOP app. If you run with a GPS watch or phone app
that uploads to Strava, use DATA_SOURCES=strava,whoop: Strava supplies the
runs (distance, pace, moving time) and WHOOP fills in everything else.

One-time setup (about 5 minutes):
  1. https://developer-dashboard.whoop.com -> sign in with your WHOOP account
     -> create a Team (any name) -> create an App.
       Scopes:        read:recovery, read:cycles, read:workout, read:sleep,
                      read:body_measurement
       Redirect URL:  http://localhost:8080/callback
     (any URL works as long as WHOOP_REDIRECT_URI below matches it exactly)
  2. Put the Client ID + Client Secret in .env:
       DATA_SOURCES=whoop
       WHOOP_CLIENT_ID=
       WHOOP_CLIENT_SECRET=
       WHOOP_REDIRECT_URI=http://localhost:8080/callback   # optional, this is the default
  3. python scripts/sync.py --source whoop --auth
     Open the printed URL, log in and approve. The browser then lands on a
     localhost page that fails to load; that's expected. Copy the WHOLE address
     from the address bar and run:
       python scripts/sync.py --source whoop --auth --code "<that address>"

Tokens: WHOOP access tokens last an hour, and every refresh issues a NEW
refresh token and invalidates the old one. The current pair is kept in
data/.whoop-token.json (gitignored; override the path with WHOOP_TOKEN_FILE).
Because of that rotation, run the WHOOP sync on ONE machine only: two machines
refreshing the same token chain log each other out. WHOOP_REFRESH_TOKEN in .env
is only used to bootstrap when the token file is missing.

Mapping notes:
  * Activities: one per WHOOP workout. duration_min is elapsed time (WHOOP has
    no moving time), training_load is WHOOP strain (0-21 scale), calories are
    WHOOP's energy expenditure (kJ / 4.184). Recovery-only entries (sauna, ice
    bath, meditation, breathwork, massage) are not training sessions and are
    skipped.
  * Wellness: dated by the morning the main (non-nap) sleep ended.
    sleep_score = WHOOP Sleep Performance %, hrv = RMSSD from the recovery,
    recovery_score = WHOOP Recovery %, day_strain and steps = the strain and
    step count of the WHOOP day that recovery belongs to.
  * WHOOP's HRV is RMSSD measured during sleep, the same statistic Garmin uses,
    but the devices measure differently: compare WHOOP only with WHOOP.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datastore import (DATA_DIR, ProviderAuthError, ProviderNotConfigured,  # noqa: E402
                       activity, canonical_type, wellness)

NAME = "whoop"
CLOUD_SAFE = True  # pure API; but see the token-rotation note above before running it on the bot

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API = "https://api.prod.whoop.com/developer/v2"
SCOPES = "offline read:recovery read:cycles read:workout read:sleep read:body_measurement"
DEFAULT_REDIRECT = "http://localhost:8080/callback"
STATE_FILE = DATA_DIR / ".whoop-auth-state"
PAGE_LIMIT = 25          # WHOOP's maximum page size
MAX_RETRIES = 3          # on 429 / 5xx

# WHOOP sport names that the generic substring rules in datastore.canonical_type miss.
# Keys are normalised: lowercase, no spaces / hyphens / underscores.
_TYPE_OVERRIDES = {
    "powerlifting": "strength",
    "calisthenics": "strength",
    "stretching": "yoga",
    "sprint": "running",
    "sprinting": "running",
    "ruck": "hiking",
    "rucking": "hiking",
    "hyrox": "hiit",
}
# Logged in WHOOP as activities, but they're recovery, not training.
_SKIP = ("sauna", "icebath", "meditation", "breathwork", "massage")


def _requests():
    try:
        import requests
        return requests
    except ImportError:
        raise ProviderNotConfigured("pip install requests")


def _client() -> tuple[str, str]:
    cid = os.getenv("WHOOP_CLIENT_ID", "").strip()
    secret = os.getenv("WHOOP_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise ProviderNotConfigured("WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET not set in .env")
    return cid, secret


def _redirect_uri() -> str:
    return os.getenv("WHOOP_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT


def _token_file() -> Path:
    p = os.getenv("WHOOP_TOKEN_FILE", "").strip()
    return Path(p).expanduser() if p else DATA_DIR / ".whoop-token.json"


# ---------- OAuth ----------

def auth(code: str | None) -> None:
    """One-time OAuth: print the consent URL, or exchange the code (or full redirect URL)."""
    cid, secret = _client()
    if not code:
        state = secrets.token_urlsafe(6)  # 8 characters: WHOOP's docs say the state "must be eight characters long"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(state, encoding="utf-8")
        params = {"client_id": cid, "response_type": "code", "redirect_uri": _redirect_uri(),
                  "scope": SCOPES, "state": state}
        print("Open this URL, log in to WHOOP and approve access:\n")
        print(f"{AUTH_URL}?{urllib.parse.urlencode(params)}\n")
        print("The browser then lands on a page that fails to load. Copy the whole address\n"
              "from the address bar and run:\n")
        print('  python scripts/sync.py --source whoop --auth --code "<that address>"')
        return

    code = code.strip()
    if "code=" in code:  # the full redirect URL was pasted
        q = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        if "error" in q:
            raise ProviderAuthError(f"WHOOP authorization was refused: {q['error'][0]}")
        expected = STATE_FILE.read_text(encoding="utf-8").strip() if STATE_FILE.exists() else None
        got = (q.get("state") or [None])[0]
        if expected and got != expected:
            raise ProviderAuthError("state mismatch: that URL is from a different --auth run. "
                                    "Run --auth again and use the new URL.")
        code = (q.get("code") or [""])[0]
    if not code:
        raise ProviderAuthError("no authorization code found in what was passed to --code")

    r = _requests().post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(),
        "client_id": cid, "client_secret": secret}, timeout=30)
    if r.status_code != 200:
        raise ProviderAuthError(f"WHOOP code exchange failed ({r.status_code}): {r.text[:200]}. "
                                "Codes are single-use and expire fast: run --auth again. Also check "
                                "that WHOOP_REDIRECT_URI matches the app's redirect URL exactly.")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise ProviderAuthError("WHOOP returned no refresh token: the 'offline' scope was not granted")
    _store(tok)
    STATE_FILE.unlink(missing_ok=True)
    print(f"Success. WHOOP is connected; tokens are saved in {_token_file()} (gitignored).")
    print("Make sure DATA_SOURCES in .env includes whoop, then run: python scripts/sync.py --start <date>")
    try:
        body = _get(tok["access_token"], "/user/measurement/body")
        print("\nWHOOP body measurements (for profile.md): "
              f"height {body.get('height_meter')} m, weight {body.get('weight_kilogram')} kg, "
              f"max HR {body.get('max_heart_rate')} bpm")
    except Exception:  # nice-to-have only
        pass


def _store(tok: dict) -> None:
    """Persist the token pair atomically: the old refresh token is already dead."""
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    expires_at = int(time.time()) + int(tok.get("expires_in") or 3600)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"access_token": tok.get("access_token"),
                               "refresh_token": tok.get("refresh_token"),
                               "expires_at": expires_at}), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _access_token() -> str:
    cid, secret = _client()
    path = _token_file()
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if saved.get("access_token") and (saved.get("expires_at") or 0) > time.time() + 120:
        return saved["access_token"]
    refresh = saved.get("refresh_token") or os.getenv("WHOOP_REFRESH_TOKEN", "").strip()
    if not refresh:
        raise ProviderNotConfigured("not connected yet: run python scripts/sync.py --source whoop --auth")
    r = _requests().post(TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": refresh,
        "client_id": cid, "client_secret": secret, "scope": "offline"}, timeout=30)
    if r.status_code in (400, 401):
        raise ProviderAuthError(f"WHOOP refresh token rejected ({r.status_code}). It may have been used "
                                "by another machine. Re-run: python scripts/sync.py --source whoop --auth")
    r.raise_for_status()
    tok = r.json()
    _store(tok)
    return tok["access_token"]


# ---------- API ----------

def _get(token: str, path: str, params: dict | None = None) -> dict:
    req = _requests()
    for attempt in range(MAX_RETRIES + 1):
        r = req.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
                    params=params, timeout=30)
        if r.status_code == 401:
            raise ProviderAuthError("WHOOP access token rejected. Re-run: "
                                    "python scripts/sync.py --source whoop --auth")
        if r.status_code == 403:
            raise ProviderAuthError(f"WHOOP refused {path} (403): a scope is missing. Check the app's "
                                    "scopes in the WHOOP developer dashboard, then re-run --auth")
        if (r.status_code == 429 or r.status_code >= 500) and attempt < MAX_RETRIES:
            wait = r.headers.get("Retry-After")
            time.sleep(min(float(wait), 60) if wait and wait.isdigit() else 2 ** (attempt + 1))
            continue
        if r.status_code == 429:
            raise RuntimeError("WHOOP rate limit hit (about 100 requests / minute): try again later")
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def _collection(token: str, path: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    params = {"start": _iso_z(start), "end": _iso_z(end), "limit": PAGE_LIMIT}
    out, seen = [], set()
    while True:
        page = _get(token, path, params)
        out += page.get("records") or []
        nxt = page.get("next_token")
        if not nxt or nxt in seen:
            return out
        seen.add(nxt)
        params["nextToken"] = nxt


def _iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse(ts: str | None) -> dt.datetime | None:
    """WHOOP timestamps: '2026-11-16T06:25:44.774Z' (UTC). Tolerates any fraction length."""
    if not ts:
        return None
    ts = ts.strip().replace("Z", "+00:00")
    if "." in ts:  # drop fractional seconds (older Pythons only accept 3 or 6 digits)
        head, rest = ts.split(".", 1)
        cut = min((rest.index(c) for c in "+-" if c in rest), default=len(rest))
        ts = head + rest[cut:]
    t = dt.datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _offset(s: str | None) -> dt.timezone:
    """'-05:00' -> timezone(-5h). Missing or odd values fall back to UTC."""
    try:
        sign = -1 if s.startswith("-") else 1
        hh, mm = s.lstrip("+-").split(":")
        return dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))
    except (AttributeError, ValueError):
        return dt.timezone.utc


def _local(ts: str | None, offset: str | None) -> dt.datetime | None:
    t = _parse(ts)
    return t.astimezone(_offset(offset)).replace(tzinfo=None) if t else None


def _norm(s: str | None) -> str:
    return (s or "").lower().replace(" ", "").replace("-", "").replace("_", "")


def _workout(w: dict) -> dict | None:
    sport = w.get("sport_name") or "activity"
    key = _norm(sport)
    if any(k in key for k in _SKIP):
        return None
    start, end = _local(w.get("start"), w.get("timezone_offset")), _local(w.get("end"), w.get("timezone_offset"))
    if start is None:
        return None
    sc = (w.get("score") or {}) if w.get("score_state") == "SCORED" else {}
    kj, dist = sc.get("kilojoule"), sc.get("distance_meter")
    return activity(
        id=w.get("id"),
        name=sport.replace("-", " ").replace("_", " ").title(),
        type=_TYPE_OVERRIDES.get(key) or canonical_type(sport),
        subtype=sport,
        start_local=start.isoformat(timespec="seconds"),
        duration_min=(end - start).total_seconds() / 60 if end else None,
        distance_km=dist / 1000 if dist else None,
        avg_hr=sc.get("average_heart_rate"),
        max_hr=sc.get("max_heart_rate"),
        calories=round(kj / 4.184) if kj else None,
        training_load=round(sc["strain"], 1) if sc.get("strain") is not None else None,
        elevation_gain_m=sc.get("altitude_gain_meter"),
    )


def _wellness(sleeps: list[dict], recoveries: list[dict], cycles: list[dict],
              start: dt.date, end: dt.date) -> list[dict]:
    # Main sleep per morning: the longest scored non-nap sleep that ended on that local date.
    main: dict[str, dict] = {}
    date_of_sleep: dict[str, str] = {}
    for s in sleeps:
        if s.get("nap"):
            continue
        e = _local(s.get("end"), s.get("timezone_offset"))
        if e is None:
            continue
        d = e.date().isoformat()
        date_of_sleep[s.get("id")] = d
        if s.get("score_state") != "SCORED":
            continue
        st = (s.get("score") or {}).get("stage_summary") or {}
        asleep = sum(st.get(k) or 0 for k in ("total_light_sleep_time_milli",
                                              "total_slow_wave_sleep_time_milli",
                                              "total_rem_sleep_time_milli"))
        if d not in main or asleep > main[d]["_asleep"]:
            main[d] = {"_asleep": asleep, "s": s}

    days: dict[str, dict] = {}
    for d, m in main.items():
        sc = m["s"].get("score") or {}
        st = sc.get("stage_summary") or {}
        days[d] = {
            "sleep_total_min": round(m["_asleep"] / 60000),
            "sleep_deep_min": round((st.get("total_slow_wave_sleep_time_milli") or 0) / 60000),
            "sleep_rem_min": round((st.get("total_rem_sleep_time_milli") or 0) / 60000),
            "sleep_score": _round(sc.get("sleep_performance_percentage")),
        }

    cycle_by_id = {c.get("id"): c for c in cycles}

    def cycle_day(c: dict) -> str | None:
        # Fallback when a cycle has no recovery yet: a WHOOP day runs sleep-to-sleep, so
        # +12 h from its start lands on the waking day whether it starts at sleep onset or waking.
        t = _local(c.get("start"), c.get("timezone_offset"))
        return (t + dt.timedelta(hours=12)).date().isoformat() if t else None

    date_of_cycle: dict = {}
    for r in recoveries:
        d = date_of_sleep.get(r.get("sleep_id"))
        if d is None and r.get("cycle_id") in cycle_by_id:
            d = cycle_day(cycle_by_id[r["cycle_id"]])
        if d is None:
            continue
        date_of_cycle[r.get("cycle_id")] = d
        if r.get("score_state") != "SCORED":
            continue
        sc = r.get("score") or {}
        day = days.setdefault(d, {})
        day["resting_hr"] = _round(sc.get("resting_heart_rate"))
        hrv = sc.get("hrv_rmssd_milli")
        day["hrv_last_night_ms"] = round(hrv, 1) if hrv is not None else None
        day["hrv_method"] = "rmssd" if hrv is not None else None
        day["recovery_score"] = _round(sc.get("recovery_score"))

    for c in cycles:
        strain = (c.get("score") or {}).get("strain") if c.get("score_state") == "SCORED" else None
        steps = c.get("step_count")
        if strain is None and steps is None:
            continue
        d = date_of_cycle.get(c.get("id")) or cycle_day(c)
        if not d:
            continue
        if strain is not None:
            days.setdefault(d, {})["day_strain"] = round(strain, 1)
        if steps is not None:
            days.setdefault(d, {})["steps"] = steps

    out = []
    for d, vals in sorted(days.items()):
        if start.isoformat() <= d <= end.isoformat():
            out.append(wellness(date=d, **vals))
    return out


def _round(v):
    return round(v) if isinstance(v, (int, float)) else None


def fetch(start: dt.date, end: dt.date, opts) -> tuple[list[dict], list[dict]]:
    token = _access_token()
    t0 = dt.datetime.combine(start, dt.time()).astimezone()
    t1 = dt.datetime.combine(end + dt.timedelta(days=1), dt.time()).astimezone()
    # Sleep / recovery / cycles from a day earlier: the night that ends on `start` began the evening before.
    t0_wide = t0 - dt.timedelta(days=1)

    workouts = _collection(token, "/activity/workout", t0, t1)
    sleeps = _collection(token, "/activity/sleep", t0_wide, t1)
    recoveries = _collection(token, "/recovery", t0_wide, t1)
    cycles = _collection(token, "/cycle", t0_wide, t1)

    acts = [a for a in (_workout(w) for w in workouts) if a]
    acts = [a for a in acts if start.isoformat() <= a["start_local"][:10] <= end.isoformat()]
    return acts, _wellness(sleeps, recoveries, cycles, start, end)
