import subprocess, json, http.server, socketserver, threading, time, urllib.request, urllib.error, os, re, calendar
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, timezone

PORT = 3333
REFRESH_INTERVAL = 300  # 5 minutes
ICAL_URL = "https://p35-caldav.icloud.com/published/2/MTA4NDU4NTMyNTEwODQ1OPWHi7SZPkNNYTKE1ACf5XyII7LHnV_Crc5J0w-OIWnVFGSspA4481xfEAUxHL4wwc0ZJR9rEDRs41PftNCEjrU"

_cache = {'events': None, 'reminders': None, 'fetching': False, 'last_fetch': 0}
_cache_lock = threading.Lock()

# iCal proxy cache — avoids hitting iCloud on every request (kept fresh ~5 min,
# served stale if iCloud is slow/unreachable so the calendar never goes blank).
_ical_cache = {'text': None, 'ts': 0, 'events': None, 'events_ts': -1}
ICAL_TTL = 300

def get_ical():
    now = time.time()
    if _ical_cache['text'] is not None and (now - _ical_cache['ts'] < ICAL_TTL):
        return _ical_cache['text']
    req = urllib.request.Request(ICAL_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read()
    _ical_cache['text'] = data
    _ical_cache['ts'] = now
    return data

# ── Server-side iCal parsing (so weak clients like a Pi Zero just render JSON) ──
def _ical_parse_dt(s):
    if not s:
        return None
    s = s.strip()
    is_utc = s.endswith('Z')
    raw = re.sub(r'[-:]', '', re.sub(r'[TZ]', '', s))
    all_day = 'T' not in s
    if len(raw) < 8:
        return None
    try:
        yr, mo, dy = int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
        hr = int(raw[8:10]) if len(raw) >= 10 else 0
        mn = int(raw[10:12]) if len(raw) >= 12 else 0
    except ValueError:
        return None
    if is_utc:
        dt = datetime(yr, mo, dy, hr, mn, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        return {'date': dt.strftime('%Y-%m-%d'), 'time': dt.strftime('%H:%M'), 'allDay': False, 'dt': dt}
    dt = datetime(yr, mo, dy, hr, mn)
    return {'date': f'{yr:04d}-{mo:02d}-{dy:02d}', 'time': '' if all_day else f'{hr:02d}:{mn:02d}', 'allDay': all_day, 'dt': dt}

def _add_months(dt, n):
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)

_JS_WD = {'SU': 0, 'MO': 1, 'TU': 2, 'WE': 3, 'TH': 4, 'FR': 5, 'SA': 6}  # Sunday-based, matches the old JS

def _ical_expand_rrule(base, rrule, exdates):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    win_start = (today.replace(day=1) - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    win_end = today + timedelta(days=120)

    p = {}
    for part in rrule.split(';'):
        if '=' in part:
            k, v = part.split('=', 1); p[k] = v
    freq = p.get('FREQ')
    interval = int(p.get('INTERVAL') or 1)
    count = int(p['COUNT']) if p.get('COUNT') else None
    until = None
    if p.get('UNTIL'):
        u = _ical_parse_dt(p['UNTIL']); until = u['dt'] if u else None
    byday = None
    if p.get('BYDAY'):
        byday = [_JS_WD.get(re.sub(r'[-+\d]', '', d)) for d in p['BYDAY'].split(',')]
        byday = [d for d in byday if d is not None]

    def advance(d):
        if freq == 'DAILY':   return d + timedelta(days=interval)
        if freq == 'WEEKLY':  return d + timedelta(days=7 * interval)
        if freq == 'MONTHLY': return _add_months(d, interval)
        if freq == 'YEARLY':  return d.replace(year=d.year + interval)
        return None

    results = []
    cur = base['dt']
    if count is None:
        guard = 0
        while cur < win_start and guard < 6000:
            nxt = advance(cur)
            if not nxt or nxt == cur: break
            cur = nxt; guard += 1
    n = 0
    while cur <= win_end:
        if (count is not None and n >= count) or (until and cur > until):
            break
        if byday and freq == 'WEEKLY':
            js_dow = (cur.weekday() + 1) % 7           # Sunday=0
            sunday = cur - timedelta(days=js_dow)
            cands = [sunday + timedelta(days=dow) for dow in byday]
        else:
            cands = [cur]
        for cd in cands:
            if cd < win_start or cd > win_end:
                continue
            ds = cd.strftime('%Y-%m-%d')
            if ds not in exdates:
                ev = dict(base); ev['id'] = f"{base['id']}_{ds}"; ev['date'] = ds
                results.append(ev)
        nxt = advance(cur)
        if not nxt or nxt == cur: break
        cur = nxt; n += 1
        if n > 1000: break
    return results

def parse_ical_events(text):
    unfolded = re.sub(r'\r\n[ \t]', '', text).replace('\r\n', '\n').replace('\r', '\n')
    out = []
    blocks = unfolded.split('BEGIN:VEVENT')
    for i in range(1, len(blocks)):
        block = blocks[i]
        def get(key):
            m = re.search(key + r'[^:\n]*:([^\n]+)', block)
            return m.group(1).strip() if m else ''
        title = get('SUMMARY').replace('\\,', ',').replace('\\n', ' ').replace('\\;', ';')
        dtstart = get('DTSTART')
        if not title or not dtstart:
            continue
        start = _ical_parse_dt(dtstart)
        if not start:
            continue
        dtend = get('DTEND')
        end = _ical_parse_dt(dtend) if dtend else None
        exdates = set()
        for v in re.findall(r'EXDATE[^:\n]*:([^\n]+)', block):
            for part in v.split(','):
                d = _ical_parse_dt(part)
                if d: exdates.add(d['date'])
        base = {
            'id': get('UID') or f'ev-{i}',
            'title': title,
            'date': start['date'],
            'time': '' if start['allDay'] else start['time'],
            'endTime': end['time'] if (end and not end['allDay']) else '',
            'location': get('LOCATION').replace('\\,', ','),
            'calendar': 'iCloud', 'source': 'apple', 'allDay': start['allDay'],
            'dt': start['dt'],
        }
        rrule = get('RRULE')
        out.extend(_ical_expand_rrule(base, rrule, exdates) if rrule else [base])
    # Trim to the visible window (a week before this month → 120 days out) so weak
    # clients aren't shipped years of old events.
    today = datetime.now()
    ws = (today.replace(day=1) - timedelta(days=7)).strftime('%Y-%m-%d')
    we = (today + timedelta(days=120)).strftime('%Y-%m-%d')
    return [{k: v for k, v in e.items() if k != 'dt'} for e in out if ws <= e['date'] <= we]

def get_ical_events():
    data = get_ical()
    if _ical_cache['events'] is not None and _ical_cache['events_ts'] == _ical_cache['ts']:
        return _ical_cache['events']
    text = data.decode('utf-8', 'ignore') if isinstance(data, (bytes, bytearray)) else data
    events = parse_ical_events(text)
    _ical_cache['events'] = events
    _ical_cache['events_ts'] = _ical_cache['ts']
    return events

# ── AppleScript helpers ───────────────────────────────────────────────────────
# IMPORTANT: scripts are passed via stdin (not -e flag) because Calendar.app
# responds to stdin-based osascript calls whereas -e causes timeouts.

# Per-calendar script template — filled in at runtime with the calendar index
CALENDAR_SCRIPT_TMPL = r"""
set lf to linefeed
set startDate to current date
set endDate to startDate + (14 * days)
set output to ""
tell application "Calendar"
  set cal to calendar INDEX_PLACEHOLDER
  set calName to name of cal
  set evList to (every event of cal whose start date >= startDate and start date <= endDate)
  repeat with ev in evList
    try
      set evTitle to summary of ev
      set evStart to start date of ev
      set evAllDay to allday event of ev
      set yr to year of evStart as integer as string
      set mo to month of evStart as integer
      set dy to day of evStart as integer
      set hr to hours of evStart as integer
      set mn to minutes of evStart as integer
      set moStr to text -2 thru -1 of ("0" & mo)
      set dyStr to text -2 thru -1 of ("0" & dy)
      set hrStr to text -2 thru -1 of ("0" & hr)
      set mnStr to text -2 thru -1 of ("0" & mn)
      set startStr to yr & "-" & moStr & "-" & dyStr & "T" & hrStr & ":" & mnStr & ":00"
      set row to evTitle & "|||" & startStr & "|||" & calName & "|||" & (evAllDay as string)
      if output is "" then
        set output to row
      else
        set output to output & lf & row
      end if
    end try
  end repeat
end tell
return output
"""

REMINDERS_SCRIPT = r"""
set lf to linefeed
set output to ""
tell application "Reminders"
  repeat with i from 1 to (count of every list)
    try
      set rl to list i
      set listName to name of rl
      set rems to (every reminder of rl whose completed is false)
      repeat with r in rems
        try
          set rTitle to name of r
          set dueStr to "none"
          try
            set rDue to due date of r
            set yr to year of rDue as integer as string
            set mo to month of rDue as integer
            set dy to day of rDue as integer
            set moStr to text -2 thru -1 of ("0" & mo)
            set dyStr to text -2 thru -1 of ("0" & dy)
            set dueStr to yr & "-" & moStr & "-" & dyStr
          end try
          set row to rTitle & "|||" & listName & "|||" & dueStr
          if output is "" then
            set output to row
          else
            set output to output & lf & row
          end if
        end try
      end repeat
    end try
  end repeat
end tell
return output
"""

def run_as(script, timeout=120):
    """Run AppleScript via stdin (not -e) — required for Calendar/Reminders to respond."""
    r = subprocess.run(
        ['osascript'],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()

def parse_events(raw):
    events = []
    for line in raw.splitlines():
        parts = line.strip().split('|||')
        if len(parts) == 4:
            events.append({
                'title': parts[0],
                'start': parts[1],
                'calendar': parts[2],
                'allDay': parts[3].lower() == 'true'
            })
    events.sort(key=lambda e: e.get('start', ''))
    return events

def parse_reminders(raw):
    rems = []

    def clean(v):
        v = v.strip()
        return v if v not in ('', 'none') else None

    for line in raw.splitlines():
        parts = line.strip().split('|||')
        # id|||title|||list|||dueDate|||dueTime|||priority|||recurrence|||notes|||tags
        if len(parts) == 9:
            tags = [t.strip() for t in parts[8].split(',') if t.strip()]
            rems.append({
                'id': parts[0],
                'title': parts[1],
                'list': parts[2],
                'due': clean(parts[3]),
                'time': clean(parts[4]),
                'priority': clean(parts[5]) or 'none',
                'recurrence': clean(parts[6]),
                'notes': clean(parts[7]),
                'tags': tags,
            })
    rems.sort(key=lambda r: (r.get('due') is None, r.get('due') or ''))
    return rems

# ── Fetch (single call per source — much faster than per-calendar threads) ────

def fetch_calendar_index(idx):
    script = CALENDAR_SCRIPT_TMPL.replace('INDEX_PLACEHOLDER', str(idx))
    raw = run_as(script, timeout=90)
    return parse_events(raw)

def fetch_all_calendar_events():
    try:
        count_script = 'tell application "Calendar" to return count of every calendar'
        count = int(run_as(count_script, timeout=10).strip())
    except Exception:
        return []

    results = []
    lock = threading.Lock()

    def fetch_one(i):
        try:
            evs = fetch_calendar_index(i)
            with lock:
                results.extend(evs)
        except Exception as e:
            pass  # calendar may have 0 events or be inaccessible

    # Limit to 4 concurrent osascript processes so Calendar.app isn't overwhelmed
    sem = threading.Semaphore(4)

    def fetch_one_guarded(i):
        with sem:
            fetch_one(i)

    threads = [threading.Thread(target=fetch_one_guarded, args=(i,), daemon=True)
               for i in range(1, count + 1)]
    for t in threads:
        t.start()
    # Deadline-based join — all threads share a single 120s budget, not 95s each
    deadline = time.time() + 120
    for t in threads:
        remaining = max(0, deadline - time.time())
        t.join(timeout=remaining)

    results.sort(key=lambda e: e.get('start', ''))
    return results

SWIFT_SCRIPT = '/Users/michaelebberts/Desktop/inner-circle-claude-code/fetch_os_data.swift'

def fetch_all_reminders():
    try:
        r = subprocess.run(
            ['swift', SWIFT_SCRIPT, 'reminders'],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            print(f'Reminders swift error: {r.stderr.strip()}', flush=True)
            return []
        return parse_reminders(r.stdout.strip())
    except Exception as e:
        print(f'Reminders error: {e}', flush=True)
        return []

def complete_reminder(reminder_id):
    """Marks an Apple Reminder done via EventKit. Returns True on success."""
    try:
        r = subprocess.run(
            ['swift', SWIFT_SCRIPT, 'complete', reminder_id],
            capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0 and r.stdout.strip().endswith('OK')
    except Exception as e:
        print(f'Complete error: {e}', flush=True)
        return False

def add_reminder(payload):
    """Creates an Apple Reminder via EventKit. Returns the new id, or None on failure."""
    try:
        r = subprocess.run(
            ['swift', SWIFT_SCRIPT, 'add'],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30
        )
        out = r.stdout.strip().splitlines()
        last = out[-1].strip() if out else ''
        if r.returncode == 0 and last and last != 'FAIL':
            return last  # the new calendarItemIdentifier
        print(f'Add failed: {r.stderr.strip()}', flush=True)
        return None
    except Exception as e:
        print(f'Add error: {e}', flush=True)
        return None

def add_reminders_batch(items):
    """Creates many Apple Reminders in one Swift process. Returns count created."""
    try:
        r = subprocess.run(
            ['swift', SWIFT_SCRIPT, 'addbatch'],
            input=json.dumps({'items': items}), capture_output=True, text=True, timeout=60
        )
        out = r.stdout.strip().splitlines()
        last = out[-1].strip() if out else '0'
        try:
            return int(last)
        except ValueError:
            print(f'Batch add failed: {r.stderr.strip()}', flush=True)
            return 0
    except Exception as e:
        print(f'Batch add error: {e}', flush=True)
        return 0

def add_calendar_event(payload):
    """Creates an Apple Calendar event via osascript. Returns True on success."""
    title    = payload.get('title', '').replace('"', "'")
    date_str = payload.get('date', '')          # YYYY-MM-DD
    time_str = payload.get('time', '')          # HH:mm  (may be empty)
    end_str  = payload.get('endTime', '')
    location = payload.get('location', '').replace('"', "'")
    cal_name = payload.get('calendar', 'Family').replace('"', "'")
    notes    = payload.get('notes', '').replace('"', "'")

    if not title or not date_str:
        return False

    # Build start/end datetime strings for AppleScript
    # AppleScript date format: "MM/DD/YYYY HH:MM:SS"
    try:
        yr, mo, dy = date_str.split('-')
    except Exception:
        return False

    sh, sm = (time_str.split(':') if ':' in time_str else ('09', '00'))
    start_dt = f"{mo}/{dy}/{yr} {sh}:{sm}:00"

    if end_str and ':' in end_str:
        eh, em = end_str.split(':')
        end_dt = f"{mo}/{dy}/{yr} {eh}:{em}:00"
    else:
        # Default 1-hour duration
        end_dt = f"{mo}/{dy}/{yr} {int(sh)+1:02d}:{sm}:00"

    loc_line  = f'set location of newEv to "{location}"' if location else ''
    note_line = f'set description of newEv to "{notes}"' if notes else ''
    script = f'''tell application "Calendar"
    set tCal to first calendar whose name is "{cal_name}"
    set startDate to date "{start_dt}"
    set endDate to date "{end_dt}"
    set newEv to make new event at end of events of tCal
    set summary of newEv to "{title}"
    set start date of newEv to startDate
    set end date of newEv to endDate
    {loc_line}
    {note_line}
end tell'''
    try:
        r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f'Calendar add error: {r.stderr.strip()}', flush=True)
            return False
        return True
    except Exception as e:
        print(f'Calendar add exception: {e}', flush=True)
        return False

# ── Background refresh ────────────────────────────────────────────────────────

def refresh_cache():
    with _cache_lock:
        if _cache['fetching']:
            return
        _cache['fetching'] = True

    print('Fetching Calendar & Reminders…', flush=True)
    try:
        events = fetch_all_calendar_events()
        reminders = fetch_all_reminders()
        with _cache_lock:
            _cache['events'] = events
            _cache['reminders'] = reminders
            _cache['last_fetch'] = time.time()
        print(f'Done — {len(events)} events, {len(reminders)} reminders', flush=True)
        threading.Thread(target=push_to_firestore, args=(events, reminders), daemon=True).start()
    except Exception as e:
        print(f'Refresh error: {e}', flush=True)
    finally:
        with _cache_lock:
            _cache['fetching'] = False

# ── iCloud public shared album (unofficial Shared Streams API) ────────────────

def _icloud_post(host, token, path, body):
    url = f'https://{host}/{token}/sharedstreams/{path}'
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'text/plain;charset=UTF-8',
        'Origin': 'https://www.icloud.com',
        'User-Agent': 'Mozilla/5.0',
    }, method='POST')
    return urllib.request.urlopen(req, timeout=20)

def fetch_icloud_album(token):
    """Returns a list of {url, portrait} for a public iCloud shared album token."""
    # The token after the '#' in https://www.icloud.com/sharedalbum/#<token>
    token = token.strip().split('#')[-1].strip('/').split('/')[-1]
    host = 'p01-sharedstreams.icloud.com'

    # webstream — any partition replies 330 with the correct host; follow it.
    stream = None
    for _ in range(4):
        try:
            r = _icloud_post(host, token, 'webstream', {'streamCtag': None})
            stream = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 330:
                try:
                    body = json.loads(e.read())
                except Exception:
                    body = {}
                host = e.headers.get('X-Apple-MMe-Host') or body.get('X-Apple-MMe-Host') or host
                continue
            raise
    if not stream:
        return []

    photos = stream.get('photos', [])
    guids = [p['photoGuid'] for p in photos if p.get('photoGuid')]
    # Pick the largest derivative per photo, remembering its dimensions
    chosen = {}  # checksum -> (width, height)
    for p in photos:
        ders = (p.get('derivatives') or {}).values()
        best = max(ders, key=lambda d: int(d.get('width') or 0), default=None)
        if best and best.get('checksum'):
            chosen[best['checksum']] = (int(best.get('width') or 0), int(best.get('height') or 0))

    if not guids:
        return []
    r = _icloud_post(host, token, 'webasseturls', {'photoGuids': guids})
    items = (json.loads(r.read()) or {}).get('items', {})

    out = []
    for checksum, (w, h) in chosen.items():
        it = items.get(checksum)
        if it and it.get('url_location') and it.get('url_path'):
            out.append({'url': f"https://{it['url_location']}{it['url_path']}", 'portrait': h > w})
    return out

OURA_TOKEN    = "EF6Q7VQY5B7NEKED4CMYODVBTIBDITPH"
OURA_BASE     = "https://api.ouraring.com/v2/usercollection"
OURA_REFRESH  = 900  # poll Oura every 15 minutes
_oura_cache   = {'data': None, 'ts': 0}

def fetch_oura():
    """Pull sleep + readiness from Oura Cloud. Cached 15 min; pushes to Firestore on each refresh."""
    now = time.time()
    if _oura_cache['data'] is not None and (now - _oura_cache['ts'] < OURA_REFRESH):
        return _oura_cache['data']

    headers = {'Authorization': f'Bearer {OURA_TOKEN}'}
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    today     = datetime.now().strftime('%Y-%m-%d')

    def oura_get(path, params):
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        req = urllib.request.Request(f'{OURA_BASE}/{path}?{qs}', headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    result = {'date': yesterday}

    try:
        d = oura_get('daily_sleep', {'start_date': yesterday, 'end_date': today})
        if d.get('data'):
            entry = d['data'][-1]
            result['sleep_score'] = entry.get('score')
            result['sleep_contributors'] = entry.get('contributors', {})
    except Exception as e:
        print(f'Oura daily_sleep error: {e}', flush=True)

    try:
        d = oura_get('sleep', {'start_date': yesterday, 'end_date': today})
        sessions = [s for s in (d.get('data') or []) if s.get('day') == yesterday]
        if sessions:
            result['total_sleep_sec'] = sum(s.get('total_sleep_duration', 0) for s in sessions)
            result['deep_sleep_sec']  = sum(s.get('deep_sleep_duration', 0) for s in sessions)
            result['rem_sleep_sec']   = sum(s.get('rem_sleep_duration', 0) for s in sessions)
            hrv_vals = [s['average_hrv'] for s in sessions if s.get('average_hrv')]
            result['avg_hrv'] = round(sum(hrv_vals) / len(hrv_vals)) if hrv_vals else None
            result['resting_hr'] = min((s['lowest_heart_rate'] for s in sessions if s.get('lowest_heart_rate')), default=None)
    except Exception as e:
        print(f'Oura sleep error: {e}', flush=True)

    try:
        d = oura_get('daily_readiness', {'start_date': yesterday, 'end_date': today})
        if d.get('data'):
            entry = d['data'][-1]
            result['readiness_score'] = entry.get('score')
            result['readiness_contributors'] = entry.get('contributors', {})
    except Exception as e:
        print(f'Oura readiness error: {e}', flush=True)

    _oura_cache['data'] = result
    _oura_cache['ts']   = now
    print(f"Oura synced — sleep {result.get('sleep_score')}, readiness {result.get('readiness_score')}", flush=True)

    # Push to Firestore so iPhone/iPad see the same data without needing the bridge
    threading.Thread(target=push_oura_to_firestore, args=(result,), daemon=True).start()
    return result

def push_oura_to_firestore(data):
    """Write Oura sleep data to Firestore sync/oura so all devices see it in real-time."""
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    url = f"{FIRESTORE_BASE}/oura?key={FIRESTORE_API_KEY}"

    def fs_val(v):
        if v is None:                  return {'nullValue': None}
        if isinstance(v, bool):        return {'booleanValue': v}
        if isinstance(v, int):         return {'integerValue': str(v)}
        if isinstance(v, float):       return {'doubleValue': v}
        if isinstance(v, dict):
            return {'mapValue': {'fields': {k: fs_val(vv) for k, vv in v.items()}}}
        return {'stringValue': str(v)}

    fields = {k: fs_val(v) for k, v in data.items()}
    fields['lastSync'] = {'timestampValue': now_str}

    body = json.dumps({'fields': fields}).encode()
    req  = urllib.request.Request(url, data=body, method='PATCH',
                                   headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=15)
        print('Firestore sync/oura pushed', flush=True)
    except Exception as e:
        print(f'Firestore oura push error: {e}', flush=True)

def schedule_oura_refresh():
    """Background loop: re-fetch Oura every 15 minutes so Firestore stays current."""
    try:
        fetch_oura()
    except Exception as e:
        print(f'Oura refresh error: {e}', flush=True)
    t = threading.Timer(OURA_REFRESH, schedule_oura_refresh)
    t.daemon = True
    t.start()

FIRESTORE_API_KEY = "AIzaSyDINHNV1Ze3QfhXwBPwe22LnUe-xxnU-n4"
FIRESTORE_BASE = "https://firestore.googleapis.com/v1/projects/inner-circle-crm/databases/(default)/documents/users/owner-inner-circle-crm/sync"

def push_to_firestore(events, reminders):
    """Write calendar+reminders to Firestore so iPhone reads them when the bridge isn't reachable."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Calendar: title|||date|||startTime|||endTime|||location|||calendar
    cal_lines = []
    for e in events:
        cal_lines.append('|||'.join([
            e.get('title', ''), e.get('date', ''), e.get('time', ''),
            e.get('endTime', ''), e.get('location', ''), e.get('calendar', 'iCloud')
        ]))

    # Reminders: title|||dueDate|||priority|||list|||notes
    rem_lines = []
    for r in reminders:
        rem_lines.append('|||'.join([
            r.get('title', ''), r.get('due') or '', r.get('priority') or 'none',
            r.get('list', ''), r.get('notes') or ''
        ]))

    def patch(suffix, data_str):
        url = f"{FIRESTORE_BASE}/{suffix}?key={FIRESTORE_API_KEY}"
        body = json.dumps({
            "fields": {
                "data":     {"stringValue": data_str},
                "lastSync": {"timestampValue": now},
            }
        }).encode()
        req = urllib.request.Request(url, data=body, method='PATCH',
                                     headers={'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(req, timeout=15)
            print(f'Firestore sync/{suffix} pushed', flush=True)
        except Exception as e:
            print(f'Firestore push error ({suffix}): {e}', flush=True)

    patch('calendar',  '\n'.join(cal_lines))
    patch('reminders', '\n'.join(rem_lines))

def push_ical_to_firestore():
    """Push iCal events (the real source of truth) to Firestore for mobile sync."""
    try:
        events = get_ical_events()
        reminders = _cache.get('reminders') or []
        push_to_firestore(events, reminders)
    except Exception as e:
        print(f'iCal Firestore push error: {e}', flush=True)

# ── Apple Health ──────────────────────────────────────────────────────────────

HEALTH_BINARY  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fetch_health_data')
HEALTH_REFRESH = 1800   # 30 minutes
HEALTH_FS_URL  = "https://firestore.googleapis.com/v1/projects/inner-circle-crm/databases/(default)/documents/users/owner-inner-circle-crm/sync/apple_health?key=AIzaSyDINHNV1Ze3QfhXwBPwe22LnUe-xxnU-n4"
_health_cache  = {'data': None, 'ts': 0}

def fetch_apple_health():
    """Run the compiled HealthKit binary and return parsed JSON. Cached 30 min."""
    now = time.time()
    if _health_cache['data'] and (now - _health_cache['ts'] < HEALTH_REFRESH):
        return _health_cache['data']
    if not os.path.exists(HEALTH_BINARY):
        return {'ok': False, 'error': 'binary_missing'}
    try:
        result = subprocess.run(
            [HEALTH_BINARY], capture_output=True, text=True, timeout=8
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {'ok': False, 'error': result.stderr.strip() or 'no output'}
        data = json.loads(result.stdout.strip())
        if data.get('ok'):
            _health_cache['data'] = data
            _health_cache['ts']   = now
            threading.Thread(target=push_health_to_firestore, args=(data,), daemon=True).start()
        return data
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'timeout — grant Health permission in System Settings first'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def push_health_to_firestore(data):
    """Push Apple Health snapshot to Firestore for cross-device access."""
    try:
        fields = {}
        for k, v in data.items():
            if k == 'ok': continue
            if isinstance(v, int):   fields[k] = {'integerValue': str(v)}
            elif isinstance(v, float): fields[k] = {'doubleValue': v}
            elif isinstance(v, str):   fields[k] = {'stringValue': v}
            elif isinstance(v, list):
                # steps_7day array
                items = []
                for item in v:
                    item_fields = {}
                    for ik, iv in item.items():
                        if isinstance(iv, int):  item_fields[ik] = {'integerValue': str(iv)}
                        elif isinstance(iv, str): item_fields[ik] = {'stringValue': iv}
                    items.append({'mapValue': {'fields': item_fields}})
                fields[k] = {'arrayValue': {'values': items}}
        fields['lastSync'] = {'stringValue': datetime.now(timezone.utc).isoformat()}
        body = json.dumps({'fields': fields}).encode()
        req  = urllib.request.Request(HEALTH_FS_URL, data=body, method='PATCH',
                                      headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
        print('Apple Health pushed to Firestore', flush=True)
    except Exception as e:
        print(f'Apple Health Firestore push error: {e}', flush=True)

def schedule_refresh():
    refresh_cache()
    # Also push iCal events (separate from AppleScript cache) to Firestore
    threading.Thread(target=push_ical_to_firestore, daemon=True).start()
    t = threading.Timer(REFRESH_INTERVAL, schedule_refresh)
    t.daemon = True
    t.start()

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.path == '/reminders/complete':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception:
                self.send_json({'error': 'invalid json'}, 400)
                return
            rid = data.get('id', '')
            if not rid:
                self.send_json({'error': 'no id'}, 400)
                return
            ok = complete_reminder(rid)
            if ok:
                # Drop it from the cache so the next fetch doesn't re-show it
                with _cache_lock:
                    if _cache['reminders']:
                        _cache['reminders'] = [r for r in _cache['reminders'] if r.get('id') != rid]
            self.send_json({'ok': ok})
            return
        if self.path == '/reminders/add':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception:
                self.send_json({'error': 'invalid json'}, 400)
                return
            title = (data.get('title') or '').strip()
            if not title:
                self.send_json({'error': 'no title'}, 400)
                return
            payload = {
                'title': title,
                'list': data.get('list') or '',
                'due': data.get('due') or '',
                'time': data.get('time') or '',
                'priority': data.get('priority') or 'none',
                'notes': data.get('notes') or '',
            }
            new_id = add_reminder(payload)
            if new_id:
                # Add to cache so it shows immediately on the next poll
                new_rem = {
                    'id': new_id, 'title': title, 'list': payload['list'] or 'Reminders',
                    'due': payload['due'] or None, 'time': payload['time'] or None,
                    'priority': payload['priority'], 'recurrence': None,
                    'notes': payload['notes'] or None, 'tags': [],
                }
                with _cache_lock:
                    if _cache['reminders'] is not None:
                        _cache['reminders'] = sorted(
                            _cache['reminders'] + [new_rem],
                            key=lambda r: (r.get('due') is None, r.get('due') or '')
                        )
                self.send_json({'ok': True, 'id': new_id, 'reminder': new_rem})
            else:
                self.send_json({'ok': False, 'error': 'add failed'}, 500)
            return
        if self.path == '/calendar/add':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception:
                self.send_json({'error': 'invalid json'}, 400)
                return
            ok = add_calendar_event(data)
            if ok:
                # Trigger Firestore push after a short delay so the new event shows on all devices
                threading.Thread(target=lambda: (
                    __import__('time').sleep(3),
                    push_ical_to_firestore()
                ), daemon=True).start()
            self.send_json({'ok': ok})
            return
        if self.path == '/reminders/add-batch':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception:
                self.send_json({'error': 'invalid json'}, 400)
                return
            items = [it for it in (data.get('items') or []) if (it.get('title') or '').strip()]
            if not items:
                self.send_json({'error': 'no items'}, 400)
                return
            count = add_reminders_batch(items)
            self.send_json({'ok': count > 0, 'count': count})
            return
        if self.path == '/ai':
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except Exception:
                self.send_json({'error': 'invalid json'}, 400)
                return
            api_key = data.pop('api_key', '')
            if not api_key:
                self.send_json({'error': 'no api key'}, 400)
                return
            try:
                req = urllib.request.Request(
                    'https://api.anthropic.com/v1/messages',
                    data=json.dumps(data).encode(),
                    headers={
                        'Content-Type': 'application/json',
                        'x-api-key': api_key,
                        'anthropic-version': '2023-06-01',
                    },
                    method='POST'
                )
                # Generous timeout — large generations (e.g. a full meal plan) can take 60s+
                with urllib.request.urlopen(req, timeout=180) as r:
                    self.send_json(json.loads(r.read()))
            except urllib.error.HTTPError as e:
                msg = e.read().decode()
                print(f'AI HTTP {e.code}: {msg[:300]}', flush=True)
                self.send_json({'error': msg}, e.code)
            except Exception as e:
                print(f'AI error: {e}', flush=True)
                self.send_json({'error': str(e)}, 500)
        else:
            self.send_json({'error': 'Not found'}, 404)

    def send_text(self, body, content_type='text/plain', status=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(b))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == '/ical':
            try:
                data = get_ical()
                self.send_text(data, 'text/calendar; charset=utf-8')
            except Exception as e:
                if _ical_cache['text'] is not None:
                    self.send_text(_ical_cache['text'], 'text/calendar; charset=utf-8')  # serve stale
                else:
                    print(f'iCal error: {e}', flush=True)
                    self.send_json({'ok': False, 'error': str(e)}, 500)
            return

        if route == '/calendar-events':
            try:
                self.send_json({'ok': True, 'events': get_ical_events()})
            except Exception as e:
                # serve last-good parse if available
                if _ical_cache.get('events') is not None:
                    self.send_json({'ok': True, 'events': _ical_cache['events']})
                else:
                    print(f'calendar-events error: {e}', flush=True)
                    self.send_json({'ok': False, 'error': str(e)}, 500)
            return

        if route == '/photos':
            album = (parse_qs(parsed.query).get('album') or [''])[0]
            if not album:
                self.send_json({'ok': False, 'error': 'no album'}, 400)
                return
            try:
                urls = fetch_icloud_album(album)
                self.send_json({'ok': True, 'photos': urls})
            except Exception as e:
                print(f'Photos error: {e}', flush=True)
                self.send_json({'ok': False, 'error': str(e)}, 500)
            return

        with _cache_lock:
            events = _cache['events']
            reminders = _cache['reminders']
            fetching = _cache['fetching']

        if route == '/calendar':
            if events is None:
                self.send_json({'ok': False, 'loading': fetching, 'error': 'Still loading…'})
            else:
                self.send_json({'ok': True, 'events': events})
        elif route == '/reminders':
            if reminders is None:
                self.send_json({'ok': False, 'loading': fetching, 'error': 'Still loading…'})
            else:
                self.send_json({'ok': True, 'reminders': reminders})
        elif route == '/health':
            self.send_json({'ok': True, 'loaded': events is not None, 'fetching': fetching})
        elif route == '/apple-health':
            self.send_json(fetch_apple_health())
        elif route == '/oura':
            try:
                data = fetch_oura()
                self.send_json({'ok': True, **data})
            except Exception as e:
                print(f'Oura route error: {e}', flush=True)
                self.send_json({'ok': False, 'error': str(e)}, 500)
        else:
            self.send_json({'error': 'Not found'}, 404)

# ── Start ─────────────────────────────────────────────────────────────────────

print(f'Inner Circle OS bridge on http://localhost:{PORT}', flush=True)
# KITCHEN_MODE (e.g. on the Raspberry Pi) skips the macOS EventKit refresh —
# only the HTTP proxies (/ical, /ai, /photos) run, which work cross-platform.
if os.environ.get('KITCHEN_MODE'):
    print('KITCHEN_MODE — EventKit refresh disabled; HTTP proxies only.', flush=True)
else:
    print('First load takes ~60-90s while Calendar and Reminders respond…', flush=True)
    threading.Thread(target=schedule_refresh, daemon=True).start()

# Oura runs in all modes (no macOS permission required — it's a cloud API call)
def _delayed_oura_start():
    import time; time.sleep(5)  # wait for server to finish starting
    schedule_oura_refresh()
threading.Thread(target=_delayed_oura_start, daemon=True).start()

# Reuse the address so a quick restart doesn't fail with "Address already in use" (TIME_WAIT).
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('', PORT), Handler) as httpd:
    httpd.serve_forever()
