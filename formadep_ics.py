"""Build an iCalendar (.ics) file from the public FORMADEP timetable page.

Uses only the Python standard library so it runs anywhere (including GitHub Actions).
"""
import argparse
import hashlib
import html
import http.cookiejar
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://www.formadep360.fr"

# Grid geometry of multi_edt (pixels)
X_ORIGIN = 106      # x position of 08:00 (half-hour grid lines at 158, 210, ... 730 = 14:00)
PX_PER_HOUR = 104
DAY_HEIGHT = 72     # one day row = two group sub-rows of 36px
GROUP_HEIGHT = 36

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Paris
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


class Client:
    def __init__(self, dep, promo):
        self.page_url = f"{BASE}/Extra/extra_edt?dep={dep}"
        self.frame_url = f"{BASE}/multi/multi_edt?type=2&promo={promo}"
        self.promo = str(promo)
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.opener.addheaders = [("User-Agent", "Mozilla/5.0 (edt-ics)")]

    def _open(self, url, data=None, referer=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, body, headers={"Referer": referer or self.page_url})
        with self.opener.open(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    def start(self):
        page = self._open(self.page_url)
        form = hidden_fields(page)
        form.update({"__EVENTTARGET": "modpromo", "modpromo": self.promo})
        self._open(self.page_url, form)
        return self._open(self.frame_url)

    def week(self, frame_html, button_name, value):
        form = hidden_fields(frame_html)
        form[button_name] = value
        return self._open(self.frame_url, form, referer=self.frame_url)


def hidden_fields(page):
    return {html.unescape(k): html.unescape(v) for k, v in
            re.findall(r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"', page)}


def week_buttons(frame_html):
    """[(button_name, week_number, monday_date)]"""
    out = []
    for name, value, day, month, year in re.findall(
            r'<input type="submit" name="(rptsemaines\$[^"]+)" value="(\d+)" '
            r'title="Du lundi (\d\d)/(\d\d)/(\d\d)', frame_html):
        out.append((name, value, datetime(2000 + int(year), int(month), int(day))))
    return out


def clean(text):
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return "\n".join(re.sub(r"\s+", " ", line).strip() for line in text.split("\n") if line.strip())


def parse_week(frame_html, monday, group):
    h = re.sub(r"\s+", " ", frame_html)
    # day rows: top pixel -> date
    rows = []
    for top, label in re.findall(r"top:\s*(\d+)px;[^>]*><table class='LayerDay'[^>]*><tr><td[^>]*>[a-z]+\.<BR>(\d\d/\d\d)", h):
        d, m = map(int, label.split("/"))
        year = monday.year + (1 if m < monday.month - 6 else 0)
        rows.append((int(top), datetime(year, m, d)))
    # which sub-row (0 = first group, 1 = second group) is ours
    groups = re.findall(r"class='LayerDay'.*?class='tob'>([^<]+)</td>.*?class='tob'>([^<]+)</td>", h)
    sub = 0
    if groups:
        names = [g.strip() for g in groups[0]]
        sub = names.index(group) if group in names else 0

    events, seen = [], set()
    for div_id, style, inner in re.findall(r'<div class="dd" id="([^"]+)" style="([^"]+)">(.*?)</div>', h):
        px = {k: int(v) for k, v in re.findall(r"(left|top|width|height):\s*(\d+)px", style)}
        if not {"left", "top", "width", "height"} <= px.keys():
            continue
        # find the day row containing this block
        row = next(((t, d) for t, d in rows if t <= px["top"] < t + DAY_HEIGHT + 1), None)
        if row is None:
            continue
        row_top, date = row
        lo = px["top"] - row_top
        hi = lo + px["height"]
        g_lo, g_hi = sub * GROUP_HEIGHT, (sub + 1) * GROUP_HEIGHT
        if hi <= g_lo + 2 or lo >= g_hi - 2:
            continue  # block belongs to the other group only
        start_min = round((px["left"] - X_ORIGIN) * 60 / PX_PER_HOUR / 5) * 5
        dur_min = round((px["width"] + 1) * 60 / PX_PER_HOUR / 5) * 5
        start = date + timedelta(hours=8, minutes=start_min)
        end = start + timedelta(minutes=dur_min)
        lines = clean(inner).split("\n")
        key = (start, end, tuple(lines))
        if key in seen:
            continue
        seen.add(key)
        events.append({"start": start, "end": end, "lines": lines})
    return events


def to_event(ev, stamp):
    title = re.sub(r"\s+-\s+!$", "", ev["lines"][0])
    room = ""
    m = re.match(r"^(.*?)\s+-\s+([A-Za-z]+-?\d+[A-Za-z]*\.?)$", title)
    if m:
        title, room = m.group(1).strip(), m.group(2).rstrip(".")
    detail = " ".join(ev["lines"][1:])
    kind = re.search(r"\[([^\]]+)\]", detail)
    if kind:
        title = f"{title} [{kind.group(1)}]"
    uid = hashlib.sha1(f"{ev['start']:%Y%m%dT%H%M}|{ev['lines'][0]}".encode()).hexdigest()
    fields = [
        "BEGIN:VEVENT",
        f"UID:{uid}@formadep-edt",
        f"DTSTAMP:{stamp}",
        f"DTSTART;TZID=Europe/Paris:{ev['start']:%Y%m%dT%H%M%S}",
        f"DTEND;TZID=Europe/Paris:{ev['end']:%Y%m%dT%H%M%S}",
        f"SUMMARY:{esc(title)}",
    ]
    if room:
        fields.append(f"LOCATION:{esc(room)}")
    fields.append(f"DESCRIPTION:{esc(chr(10).join(ev['lines']))}")
    fields.append("END:VEVENT")
    return fields


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    if len(line.encode("utf-8")) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def previous_events(path, before):
    """VEVENT blocks from an earlier run that start before `before` (weeks FORMADEP no longer shows)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read().replace("\r\n ", "")
    kept = []
    for block in re.findall(r"BEGIN:VEVENT\r\n(.*?)\r\nEND:VEVENT", text, re.S):
        m = re.search(r"DTSTART;TZID=Europe/Paris:(\d{8})", block)
        if m and m.group(1) < before.strftime("%Y%m%d"):
            kept.append(["BEGIN:VEVENT", *block.split("\r\n"), "END:VEVENT"])
    return kept


def build(dep, promo, group, name, previous_path):
    client = Client(dep, promo)
    frame = client.start()
    weeks = week_buttons(frame)
    events = []
    for button, value, monday in weeks:
        week_html = client.week(frame, button, value)
        found = parse_week(week_html, monday, group)
        print(f"semaine {value} ({monday:%d/%m/%Y}): {len(found)} séances")
        events.extend(found)
    if not events:
        raise SystemExit("Aucune séance trouvée : la page a peut-être changé, fichier non modifié.")
    kept = previous_events(previous_path, min(m for _, _, m in weeks))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//edt-formadep//FR", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{name}", "X-WR-TIMEZONE:Europe/Paris",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    lines += VTIMEZONE.split("\n")
    for block in kept:
        lines += block
    for ev in sorted(events, key=lambda e: e["start"]):
        lines += to_event(ev, stamp)
    lines.append("END:VCALENDAR")
    print(f"{len(kept)} anciennes séances conservées")
    return "\r\n".join(fold(l) for l in lines) + "\r\n", events


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dep", default="32")
    ap.add_argument("--promo", default="72")        # BUT Info FA3
    ap.add_argument("--group", default="FA3C")
    ap.add_argument("--name", default="BUT Info FA3C")
    ap.add_argument("--out", default="docs/fa3c.ics")
    a = ap.parse_args()
    ics, evs = build(a.dep, a.promo, a.group, a.name, a.out)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    print(f"{len(evs)} séances écrites dans {a.out}")
