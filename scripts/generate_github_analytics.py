#!/usr/bin/env python3
"""Generate the profile GitHub analytics card from GitHub API data.

This is intentionally self-contained: no third-party runtime packages.
Metrics are calculated from repository metadata, search totals, contribution
calendar data, and repository language byte counts.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import html
import urllib.parse
import urllib.request
from pathlib import Path

OWNER = "Fbi-Boy"
OUT = Path("assets/github-analytics-v2.svg")
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

if not TOKEN:
    raise SystemExit("GITHUB_TOKEN is required")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Fbi-Boy-profile-analytics",
}

def get_json(path: str, *, query: dict[str, str] | None = None):
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)

def graphql(query: str, variables: dict):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={**HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]

def esc(value) -> str:
    return html.escape(str(value), quote=True)

def fmt_num(value: int) -> str:
    return f"{value:,}"

def safe_percent(value: int, total: int) -> float:
    return (value / total * 100.0) if total else 0.0

def search_total(q: str) -> int:
    return int(get_json("/search/issues", query={"q": q, "per_page": "1"})["total_count"])

def commit_total() -> int:
    # Search uses the GitHub user identity, not an author-name string.
    return int(get_json("/search/commits", query={"q": f"author:{OWNER}", "per_page": "1"})["total_count"])

def contribution_data():
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=365)
    query = """
    query($login:String!, $from:DateTime!, $to:DateTime!) {
      user(login:$login) {
        contributionsCollection(from:$from, to:$to) {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    data = graphql(
        query,
        {
            "login": OWNER,
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{end.isoformat()}T23:59:59Z",
        },
    )
    calendar = data["user"]["contributionsCollection"]["contributionCalendar"]
    days = []
    for week in calendar["weeks"]:
        for day in week["contributionDays"]:
            days.append({
                "date": dt.date.fromisoformat(day["date"]),
                "count": int(day["contributionCount"]),
            })
    days.sort(key=lambda x: x["date"])
    return int(calendar["totalContributions"]), days, start, end

def streaks(days: list[dict]):
    by_date = {x["date"]: x["count"] for x in days}
    if not days:
        return 0, 0

    # Current streak: GitHub-style, ending today when today has activity,
    # otherwise ending yesterday.
    today = max(by_date)
    anchor = today if by_date.get(today, 0) > 0 else today - dt.timedelta(days=1)
    current = 0
    cursor = anchor
    while by_date.get(cursor, 0) > 0:
        current += 1
        cursor -= dt.timedelta(days=1)

    longest = 0
    run = 0
    previous = None
    for day in sorted(by_date):
        if by_date[day] > 0:
            if previous is not None and day == previous + dt.timedelta(days=1):
                run += 1
            else:
                run = 1
            longest = max(longest, run)
            previous = day
    return current, longest

def language_percentages(repos):
    totals: dict[str, int] = {}
    for repo in repos:
        if repo.get("fork") or repo.get("archived"):
            continue
        name = repo["name"]
        lang = get_json(f"/repos/{OWNER}/{name}/languages")
        for language, byte_count in lang.items():
            totals[language] = totals.get(language, 0) + int(byte_count)

    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    total_bytes = sum(totals.values())
    return [(name, safe_percent(count, total_bytes)) for name, count in ranked[:5]]

def ring(cx, cy, r, percent, stroke, width=7):
    circumference = 2 * 3.141592653589793 * r
    percent = min(max(percent, 0), 100)
    dash = circumference * percent / 100
    return f'''
    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#183126" stroke-width="{width}"/>
    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{stroke}" stroke-width="{width}"
            stroke-linecap="round" stroke-dasharray="{dash:.2f} {circumference:.2f}"
            transform="rotate(-90 {cx} {cy})"/>
    '''

def build_svg(repo_count, stars, commits, prs, issues, contributions, current, longest, langs, updated):
    W, H = 920, 420
    bg = "#070c0a"
    card = "#0b1410"
    border = "#284637"
    text = "#b8ffcc"
    green = "#41ff88"
    bright = "#00ff9a"
    muted = "#73b88c"
    track = "#183126"

    contribution_pct = min(100, contributions / 1100 * 100)
    record_pct = min(100, longest / 30 * 100)

    stat_items = [
        ("Total Stars Earned", fmt_num(stars)),
        ("Total Commits", fmt_num(commits)),
        ("Total Pull Requests", fmt_num(prs)),
        ("Total Issues", fmt_num(issues)),
        ("Total Repositories", fmt_num(repo_count)),
    ]

    lang_rows = []
    row_y = 301
    for name, pct in langs[:5]:
        label = f"{name} {pct:.1f}%"
        bar_w = max(3, 330 * pct / 100)
        lang_rows.append(
            f'<text x="282" y="{row_y}" font-size="11" fill="{text}">{esc(label)}</text>'
            f'<rect x="440" y="{row_y-8}" width="330" height="6" rx="3" fill="{track}"/>'
            f'<rect x="440" y="{row_y-8}" width="{bar_w:.1f}" height="6" rx="3" fill="{green}"/>'
        )
        row_y += 20

    def label_value(x, y, label, value):
        return (
            f'<text x="{x}" y="{y}" font-size="10" fill="{muted}">{esc(label)}</text>'
            f'<text x="{x}" y="{y+15}" font-size="19" font-weight="800" fill="{bright}">{esc(value)}</text>'
        )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect width="{W}" height="{H}" rx="18" fill="{bg}"/>
<rect x="12" y="12" width="896" height="396" rx="18" fill="{bg}" stroke="{border}"/>

<g font-family="Consolas, 'Courier New', monospace">
  <!-- CARD 1 -->
  <rect x="28" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="44" y="53" font-size="14" font-weight="700" fill="{green}">F4B0Y GitHub Stats</text>
  {''.join(
      f'<text x="44" y="{82+i*29}" font-size="10" fill="{muted}">{esc(k)}</text>'
      f'<text x="204" y="{82+i*29}" font-size="12" font-weight="700" fill="{bright}" text-anchor="end">{esc(v)}</text>'
      for i,(k,v) in enumerate(stat_items)
  )}
  <circle cx="258" cy="190" r="30" fill="none" stroke="{green}" stroke-width="2"/>
  <text x="258" y="187" font-size="8" fill="{muted}" text-anchor="middle">FOCUS</text>
  <text x="258" y="201" font-size="13" font-weight="800" fill="{bright}" text-anchor="middle">DEV</text>

  <!-- CARD 2 -->
  <rect x="320" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="336" y="53" font-size="14" font-weight="700" fill="{green}">Contribution Activity</text>
  {ring(405,122,48,contribution_pct,green,7)}
  <text x="405" y="117" font-size="22" font-weight="800" fill="{bright}" text-anchor="middle">{fmt_num(contributions)}</text>
  <text x="405" y="135" font-size="9" fill="{muted}" text-anchor="middle">LAST 365 DAYS</text>
  {label_value(490, 106, "CURRENT STREAK", f"{current} DAYS")}
  <text x="336" y="210" font-size="9" fill="{muted}">Source: GitHub contribution calendar</text>

  <!-- CARD 3 -->
  <rect x="612" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="628" y="53" font-size="14" font-weight="700" fill="{green}">Streak Record</text>
  {ring(697,122,48,record_pct,bright,7)}
  <text x="697" y="117" font-size="22" font-weight="800" fill="{bright}" text-anchor="middle">{longest}</text>
  <text x="697" y="135" font-size="9" fill="{muted}" text-anchor="middle">LONGEST STREAK</text>
  {label_value(782, 106, "365D CONTRIBUTIONS", fmt_num(contributions))}
  <text x="628" y="210" font-size="9" fill="{muted}">Independent streak calculation</text>

  <!-- LANGUAGE CARD -->
  <rect x="170" y="258" width="580" height="137" rx="12" fill="{card}" stroke="{border}"/>
  <text x="188" y="284" font-size="13" font-weight="700" fill="{green}">Most Used Languages</text>
  {''.join(lang_rows)}
</g>

<text x="892" y="404" font-size="8" fill="{muted}" text-anchor="end"
      font-family="Consolas, 'Courier New', monospace">F4B0Y Analytics Engine · Updated {esc(updated)}</text>
</svg>'''
    return svg

def main():
    repos = get_json(f"/users/{OWNER}/repos", query={"per_page":"100", "type":"owner", "sort":"updated"})
    stars = sum(int(repo.get("stargazers_count", 0)) for repo in repos)
    repo_count = len(repos)

    commits = commit_total()
    prs = search_total(f"author:{OWNER} type:pr")
    issues = search_total(f"author:{OWNER} type:issue")

    contributions, days, start, end = contribution_data()
    current, longest = streaks(days)
    langs = language_percentages(repos)

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    svg = build_svg(repo_count, stars, commits, prs, issues, contributions, current, longest, langs, updated)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg, encoding="utf-8")

    print(json.dumps({
        "repositories": repo_count,
        "stars": stars,
        "commits": commits,
        "pull_requests": prs,
        "issues": issues,
        "contributions_365d": contributions,
        "current_streak": current,
        "longest_streak": longest,
        "languages": langs,
        "period": f"{start}..{end}",
    }, indent=2))

if __name__ == "__main__":
    main()
