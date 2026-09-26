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
OUT = Path("assets/github-analytics-v3.svg")
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
          totalCommitContributions
          totalIssueContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
          totalRepositoryContributions
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
    collection = data["user"]["contributionsCollection"]
    calendar = collection["contributionCalendar"]
    days = []
    for week in calendar["weeks"]:
        for day in week["contributionDays"]:
            days.append({
                "date": dt.date.fromisoformat(day["date"]),
                "count": int(day["contributionCount"]),
            })
    days.sort(key=lambda x: x["date"])
    return (
        int(calendar["totalContributions"]),
        int(collection["totalCommitContributions"]),
        int(collection["totalPullRequestContributions"]),
        int(collection["totalIssueContributions"]),
        days,
        start,
        end,
    )

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
    W, H = 920, 430
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

    stats = [
        ("Stars Earned", fmt_num(stars)),
        ("Commits", fmt_num(commits)),
        ("Pull Requests", fmt_num(prs)),
        ("Issues", fmt_num(issues)),
        ("Repositories", fmt_num(repo_count)),
    ]

    stat_rows = []
    for i, (label, value) in enumerate(stats):
        yy = 89 + i * 26
        stat_rows.append(
            f'<text x="134" y="{yy}" font-size="10" fill="{muted}">{esc(label)}</text>'
            f'<text x="198" y="{yy}" font-size="11" font-weight="700" fill="{bright}" text-anchor="end">{esc(value)}</text>'
        )

    lang_rows = []
    y = 312
    for name, pct in langs[:5]:
        label = f"{name}  {pct:.1f}%"
        bar_width = 570 * pct / 100
        lang_rows.append(
            f'<text x="58" y="{y}" font-size="10" fill="{text}">{esc(label)}</text>'
            f'<rect x="220" y="{y-8}" width="570" height="7" rx="3.5" fill="{track}"/>'
            f'<rect x="220" y="{y-8}" width="{max(4, bar_width):.1f}" height="7" rx="3.5" fill="{green}"/>'
        )
        y += 20

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect width="{W}" height="{H}" rx="18" fill="{bg}"/>
<rect x="12" y="12" width="896" height="406" rx="18" fill="{bg}" stroke="{border}"/>

<g font-family="Consolas, 'Courier New', monospace">
  <!-- CARD 1 -->
  <rect x="28" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="46" y="54" font-size="14" font-weight="700" fill="{green}">F4B0Y GitHub Stats</text>
  <circle cx="82" cy="135" r="46" fill="none" stroke="{track}" stroke-width="7"/>
  <circle cx="82" cy="135" r="46" fill="none" stroke="{green}" stroke-width="3"/>
  <text x="82" y="128" font-size="8" fill="{muted}" text-anchor="middle">COMMITS</text>
  <text x="82" y="148" font-size="21" font-weight="800" fill="{bright}" text-anchor="middle">{fmt_num(commits)}</text>
  {''.join(stat_rows)}
  <text x="82" y="191" font-size="7" fill="{muted}" text-anchor="middle">TOTAL</text>

  <!-- CARD 2 -->
  <rect x="320" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="338" y="54" font-size="14" font-weight="700" fill="{green}">Contribution Activity</text>
  {ring(406,122,46,contribution_pct,green,7)}
  <text x="406" y="117" font-size="21" font-weight="800" fill="{bright}" text-anchor="middle">{fmt_num(contributions)}</text>
  <text x="406" y="134" font-size="8" fill="{muted}" text-anchor="middle">LAST 365 DAYS</text>
  <text x="489" y="104" font-size="8" fill="{muted}">CURRENT STREAK</text>
  <text x="489" y="131" font-size="23" font-weight="800" fill="{bright}">{current}</text>
  <text x="489" y="149" font-size="8" fill="{muted}">DAYS</text>
  <text x="338" y="210" font-size="8" fill="{muted}">GitHub contribution calendar</text>

  <!-- CARD 3 -->
  <rect x="612" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="630" y="54" font-size="14" font-weight="700" fill="{green}">Streak Record</text>
  {ring(697,122,46,record_pct,bright,7)}
  <text x="697" y="117" font-size="21" font-weight="800" fill="{bright}" text-anchor="middle">{longest}</text>
  <text x="697" y="134" font-size="8" fill="{muted}" text-anchor="middle">LONGEST STREAK</text>
  <text x="780" y="104" font-size="8" fill="{muted}">CONTRIBUTIONS</text>
  <text x="780" y="131" font-size="23" font-weight="800" fill="{bright}">{fmt_num(contributions)}</text>
  <text x="780" y="149" font-size="8" fill="{muted}">LAST 365 DAYS</text>
  <text x="630" y="210" font-size="8" fill="{muted}">Independent streak calculation</text>

  <!-- CARD 4 -->
  <rect x="28" y="258" width="864" height="160" rx="12" fill="{card}" stroke="{border}"/>
  <text x="46" y="285" font-size="14" font-weight="700" fill="{green}">Most Used Languages</text>
  <text x="874" y="285" font-size="8" fill="{muted}" text-anchor="end">Repository language bytes</text>
  <line x1="46" y1="294" x2="874" y2="294" stroke="{border}"/>
  {''.join(lang_rows)}
</g>

<text x="46" y="431" font-size="8" fill="{muted}"
      font-family="Consolas, 'Courier New', monospace">F4B0Y Analytics Engine · Updated {esc(updated)}</text>
</svg>'''
    return svg
def main():
    repos = get_json(f"/users/{OWNER}/repos", query={"per_page":"100", "type":"owner", "sort":"updated"})
    stars = sum(int(repo.get("stargazers_count", 0)) for repo in repos)
    repo_count = len(repos)

    (
        contributions,
        commits,
        prs,
        issues,
        days,
        start,
        end,
    ) = contribution_data()
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
        "metric_scope": "GitHub contribution collection for the last year",
    }, indent=2))

if __name__ == "__main__":
    main()
