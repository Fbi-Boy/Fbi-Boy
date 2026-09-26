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
OUT = Path("assets/github-analytics.svg")
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

def ring(cx, cy, r, value, label, stroke="#7dd3fc"):
    circumference = 2 * 3.141592653589793 * r
    dash = circumference * min(max(value, 0), 100) / 100
    return f'''
    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#28323d" stroke-width="6"/>
    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{stroke}" stroke-width="6"
            stroke-linecap="round" stroke-dasharray="{dash:.2f} {circumference:.2f}"
            transform="rotate(-90 {cx} {cy})"/>
    '''

def build_svg(repo_count, stars, commits, prs, issues, contributions, current, longest, langs, updated):
    W, H = 920, 330
    bg = "#0d1117"
    card = "#11161d"
    border = "#3a4652"
    text = "#f0f6fc"
    muted = "#8b949e"
    cyan = "#7dd3fc"
    cyan2 = "#67e8f9"

    # Streak ring percentages are normalized so the ring is visual only;
    # the number beside it is the actual calculated streak.
    cur_ring = min(100, current * 10)
    long_ring = min(100, longest * 5)

    lang_rows = []
    y0 = 277
    for idx, (name, pct) in enumerate(langs[:5]):
        y = y0 + idx * 10
        label = f"{name} {pct:.1f}%"
        width = max(2, 255 * pct / 100)
        lang_rows.append(
            f'<text x="356" y="{y}" font-size="9" fill="{text}">{esc(label)}</text>'
            f'<rect x="356" y="{y+3}" width="255" height="4" rx="2" fill="#28323d"/>'
            f'<rect x="356" y="{y+3}" width="{width:.1f}" height="4" rx="2" fill="{cyan2}"/>'
        )

    stat_items = [
        ("Total Stars Earned", fmt_num(stars)),
        ("Total Commits", fmt_num(commits)),
        ("Total Pull Requests", fmt_num(prs)),
        ("Total Issues", fmt_num(issues)),
        ("Total Repositories", fmt_num(repo_count)),
    ]

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect width="{W}" height="{H}" rx="14" fill="{bg}"/>
<rect x="12" y="12" width="896" height="306" rx="14" fill="{bg}" stroke="{border}"/>

<!-- left stats -->
<rect x="28" y="28" width="300" height="196" rx="10" fill="{card}" stroke="{border}"/>
<text x="44" y="49" font-size="13" font-weight="700" fill="{text}">F4B0Y GitHub Stats</text>
{''.join(f'<text x="44" y="{70+i*29}" font-size="9" fill="{muted}">{esc(k)}</text><text x="180" y="{70+i*29}" font-size="11" font-weight="700" fill="{text}" text-anchor="end">{esc(v)}</text>' for i,(k,v) in enumerate(stat_items))}
<circle cx="274" cy="105" r="36" fill="none" stroke="{cyan}" stroke-width="2"/>
<text x="274" y="102" font-size="10" fill="{muted}" text-anchor="middle">Focus</text>
<text x="274" y="118" font-size="18" font-weight="800" fill="{text}" text-anchor="middle">DEV</text>

<!-- contributions -->
<rect x="338" y="28" width="272" height="196" rx="10" fill="{card}" stroke="{border}"/>
<text x="354" y="49" font-size="13" font-weight="700" fill="{text}">Contribution Activity</text>
<circle cx="430" cy="118" r="47" fill="none" stroke="#28323d" stroke-width="6"/>
{ring(430,118,47,min(100, contributions/5),cyan)}
<text x="430" y="113" font-size="22" font-weight="800" fill="{text}" text-anchor="middle">{fmt_num(contributions)}</text>
<text x="430" y="130" font-size="9" fill="{muted}" text-anchor="middle">Last 365 Days</text>
<text x="520" y="105" font-size="9" fill="{muted}" text-anchor="middle">Current Streak</text>
<text x="520" y="132" font-size="21" font-weight="800" fill="{text}" text-anchor="middle">{current}</text>
<text x="520" y="151" font-size="9" fill="{muted}" text-anchor="middle">days</text>
<text x="354" y="191" font-size="9" fill="{muted}">Calculated from GitHub contribution calendar</text>

<!-- longest streak -->
<rect x="620" y="28" width="272" height="196" rx="10" fill="{card}" stroke="{border}"/>
<text x="636" y="49" font-size="13" font-weight="700" fill="{text}">Streak Record</text>
{ring(710,116,47,long_ring,cyan2)}
<text x="710" y="112" font-size="22" font-weight="800" fill="{text}" text-anchor="middle">{longest}</text>
<text x="710" y="130" font-size="9" fill="{muted}" text-anchor="middle">Longest Streak</text>
<text x="812" y="105" font-size="9" fill="{muted}" text-anchor="middle">365D</text>
<text x="812" y="131" font-size="16" font-weight="800" fill="{text}" text-anchor="middle">{fmt_num(contributions)}</text>
<text x="812" y="151" font-size="9" fill="{muted}" text-anchor="middle">contributions</text>
<text x="636" y="191" font-size="9" fill="{muted}">Independent streak calculation</text>

<!-- languages -->
<rect x="250" y="240" width="420" height="62" rx="10" fill="{card}" stroke="{border}"/>
<text x="266" y="259" font-size="12" font-weight="700" fill="{text}">Most Used Languages</text>
{''.join(lang_rows)}

<text x="892" y="312" font-size="8" fill="{muted}" text-anchor="end">F4B0Y Analytics Engine · Updated {esc(updated)}</text>
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
