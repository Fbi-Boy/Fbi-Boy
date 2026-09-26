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
import re
import urllib.parse
import urllib.request
from pathlib import Path

OWNER = "Fbi-Boy"
OUT = Path("assets/github-analytics-v10.svg")
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

# GitHub does not publish an official "expert" score. This transparent
# reference pool uses well-known, high-impact open-source maintainer accounts
# and compares the exact profile-style stats shown in this card.
RANK_BENCHMARK_USERS = ("torvalds", "tj", "sindresorhus", "gaearon")

def benchmark_profile_stats():
    result = []
    for login in RANK_BENCHMARK_USERS:
        profile = get_json(f"/users/{login}")
        repos_count = int(profile.get("public_repos", 0))
        total_stars = 0
        page = 1
        while True:
            repos = get_json(
                f"/users/{login}/repos",
                query={
                    "per_page": "100",
                    "page": str(page),
                    "type": "owner",
                    "sort": "updated",
                },
            )
            for repo in repos:
                if not repo.get("fork") and not repo.get("archived"):
                    total_stars += int(repo.get("stargazers_count", 0))
            if len(repos) < 100:
                break
            page += 1
            if page > 20:
                break

        commits = int(get_json(
            "/search/commits",
            query={"q": f"author:{login}", "per_page": "1"}
        )["total_count"])
        prs = search_total(f"author:{login} type:pr")
        issues = search_total(f"author:{login} type:issue")

        result.append({
            "login": login,
            "stars": total_stars,
            "commits": commits,
            "pull_requests": prs,
            "issues": issues,
            "repositories": repos_count,
        })
    return result

def rank_developer(stars, commits, prs, issues, repositories, benchmarks):
    import math

    fields = {
        "stars": 0.20,
        "commits": 0.30,
        "pull_requests": 0.25,
        "issues": 0.15,
        "repositories": 0.10,
    }
    values = {
        "stars": stars,
        "commits": commits,
        "pull_requests": prs,
        "issues": issues,
        "repositories": repositories,
    }
    ceilings = {
        field: max([int(b.get(field, 0)) for b in benchmarks] + [1])
        for field in fields
    }

    score = 0.0
    for field, weight in fields.items():
        value = max(0, int(values[field]))
        ceiling = ceilings[field]
        normalized = min(1.0, math.log1p(value) / math.log1p(ceiling))
        score += normalized * weight
    score *= 100.0

    tiers = [
        (10.0, "-F"), (15.0, "F"), (20.0, "F+"),
        (25.0, "-D"), (30.0, "D"), (35.0, "D+"),
        (40.0, "-C"), (45.0, "C"), (50.0, "C+"),
        (55.0, "-B"), (60.0, "B"), (65.0, "B+"),
        (70.0, "-A"), (75.0, "A"), (80.0, "A+"),
        (85.0, "-S"), (88.0, "S"), (92.0, "S+"),
        (95.0, "SS"), (97.0, "SS+"), (99.0, "SSS"), (100.0, "SSS+"),
    ]
    rank = "-F"
    for threshold, label in tiers:
        if score >= threshold:
            rank = label

    return rank, round(score, 2), ceilings

def contribution_data():
    # The GitHub profile calendar is the authoritative source for the number
    # displayed as "N contributions in the last year". Using this page keeps
    # the dashboard total identical to the calendar the user sees.
    req = urllib.request.Request(
        f"https://github.com/users/{OWNER}/contributions",
        headers={
            "Accept": "text/html",
            "User-Agent": HEADERS["User-Agent"],
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html_body = resp.read().decode("utf-8", errors="replace")

    total_match = re.search(
        r"(?:^|>)([0-9][0-9,]*) contributions in the last year(?:<|$)",
        html_body,
        flags=re.IGNORECASE,
    )
    if not total_match:
        # Fallback to GraphQL only if GitHub changes the public calendar HTML.
        end_date = dt.datetime.now(dt.timezone.utc).date()
        start_date = end_date - dt.timedelta(days=365)
        return contribution_data_graphql(start_date, end_date)

    total = int(total_match.group(1).replace(",", ""))

    # Daily cells contain data-date/data-count on some GitHub renderings.
    # When data-count is unavailable, keep the GraphQL daily data for streaks.
    days = []
    for match in re.finditer(
        r'<(?:td|rect)[^>]*data-date=["\'](\\d{4}-\\d{2}-\\d{2})["\'][^>]*data-count=["\'](\\d+)["\'][^>]*>',
        html_body,
        flags=re.IGNORECASE,
    ):
        days.append({
            "date": dt.date.fromisoformat(match.group(1)),
            "count": int(match.group(2)),
        })

    if days:
        days.sort(key=lambda x: x["date"])
        return total, None, None, None, None, None, days, days[0]["date"], days[-1]["date"]

    end_date = dt.datetime.now(dt.timezone.utc).date()
    start_date = end_date - dt.timedelta(days=365)
    _g_total, g_commits, g_prs, g_issues, _g_reviews, _g_repos, g_days, g_start, g_end = contribution_data_graphql(
        start_date, end_date
    )
    return total, g_commits, g_prs, g_issues, _g_reviews, _g_repos, g_days, g_start, g_end

def contribution_data_graphql(start: dt.date, end: dt.date):
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
        int(collection["totalPullRequestReviewContributions"]),
        int(collection["totalRepositoryContributions"]),
        days,
        start,
        end,
    )

def streaks(days: list[dict]):
    by_date = {x["date"]: x["count"] for x in days}
    if not days:
        return 0, 0, 0, 0

    current = 0
    end = max(by_date)
    cursor = end
    while cursor in by_date and by_date[cursor] > 0:
        current += 1
        cursor -= dt.timedelta(days=1)

    longest = 0
    run = 0
    previous = None
    for day in sorted(by_date):
        if previous is not None and day == previous + dt.timedelta(days=1) and by_date[day] > 0:
            run += 1
        elif by_date[day] > 0:
            run = 1
        else:
            run = 0
        longest = max(longest, run)
        previous = day

    active_days = sum(1 for count in by_date.values() if count > 0)
    best_day = max(by_date.values(), default=0)
    return current, longest, active_days, best_day

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

def pixel_text(text_value, cx, cy, scale=4, fill="#00ff9a"):
    """Render short rank labels as crisp pixel blocks centered on (cx, cy)."""
    glyphs = {
        "A": ["01110","10001","10001","11111","10001","10001","10001"],
        "B": ["11110","10001","10001","11110","10001","10001","11110"],
        "C": ["01111","10000","10000","10000","10000","10000","01111"],
        "D": ["11110","10001","10001","10001","10001","10001","11110"],
        "E": ["11111","10000","10000","11110","10000","10000","11111"],
        "F": ["11111","10000","10000","11110","10000","10000","10000"],
        "S": ["01111","10000","10000","01110","00001","00001","11110"],
        "+": ["00100","00100","11111","00100","00100","00000","00000"],
        "-": ["00000","00000","11111","00000","00000","00000","00000"],
    }
    chars = [glyphs.get(ch.upper(), glyphs["-"]) for ch in str(text_value)]
    glyph_width = 5 * scale
    gap_width = scale
    total_w = len(chars) * glyph_width + max(0, len(chars) - 1) * gap_width
    total_h = 7 * scale
    x0 = cx - total_w / 2
    y0 = cy - total_h / 2

    rects = []
    for glyph_index, glyph in enumerate(chars):
        base_x = x0 + glyph_index * (glyph_width + gap_width)
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    rects.append(
                        f'<rect x="{base_x + col * scale:.1f}" '
                        f'y="{y0 + row * scale:.1f}" '
                        f'width="{scale}" height="{scale}" fill="{fill}"/>'
                    )
    return "".join(rects)


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

def build_svg(repo_count, stars, commits, prs, issues, contributions, current, longest,
              calendar_prs, calendar_issues, reviews, repo_contributions,
              active_days, best_day, langs, updated, rank):
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

    def list_rows(items, x_label, x_colon, x_value, start_y=88, gap=23, max_rows=5):
        rows = []
        for i, (label, value) in enumerate(items[:max_rows]):
            yy = start_y + i * gap
            rows.append(
                f'<text x="{x_label}" y="{yy}" font-size="9.5" fill="{muted}">{esc(label)}</text>'
                f'<text x="{x_colon}" y="{yy}" font-size="9.5" fill="{muted}" text-anchor="middle">:</text>'
                f'<text x="{x_value}" y="{yy}" font-size="10.5" font-weight="700" fill="{bright}" text-anchor="end">{esc(value)}</text>'
            )
        return "".join(rows)

    stats_rows = list_rows([
        ("SE", fmt_num(stars)),
        ("Commits", fmt_num(commits)),
        ("PR", fmt_num(prs)),
        ("Issues", fmt_num(issues)),
        ("Repos", fmt_num(repo_count)),
    ], 164, 228, 292, 88, 23)

    contribution_rows = list_rows([
        ("Commits", fmt_num(calendar_commit := commits)),
        ("PR", fmt_num(calendar_prs if calendar_prs is not None else prs)),
        ("Issues", fmt_num(calendar_issues if calendar_issues is not None else issues)),
        ("Reviews", fmt_num(reviews if reviews is not None else 0)),
        ("Repos", fmt_num(repo_contributions if repo_contributions is not None else 0)),
    ], 462, 518, 584)

    streak_rows = list_rows([
        ("Current", f"{current} d"),
        ("Longest", f"{longest} d"),
        ("Active", f"{active_days} d"),
        ("Best Day", fmt_num(best_day)),
        ("Contributions", fmt_num(contributions)),
    ], 754, 810, 876)

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
  <!-- CARD 1: title centered; bottom split into left metric + right stats -->
  <rect x="28" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="168" y="55" font-size="14" font-weight="700" fill="{green}" text-anchor="middle">F4B0Y GitHub Stats</text>
  <line x1="168" y1="70" x2="168" y2="214" stroke="{card}" stroke-width="1"/>
  <circle cx="94" cy="140" r="46" fill="none" stroke="{track}" stroke-width="7"/>
  <circle cx="94" cy="140" r="46" fill="none" stroke="{green}" stroke-width="3"/>
  {pixel_text(rank, 94, 140, scale=4, fill=bright)}
  <text x="94" y="205" font-size="8" fill="{muted}" font-weight="700" text-anchor="middle">RANK</text>
  {stats_rows}

  <!-- CARD 2: title centered; bottom split into left metric + right contribution list -->
  <rect x="320" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="460" y="55" font-size="14" font-weight="700" fill="{green}" text-anchor="middle">Contribution Activity</text>
  <line x1="460" y1="70" x2="460" y2="214" stroke="{card}" stroke-width="1"/>
  {ring(392,140,46,contribution_pct,green,7)}
  <text x="392" y="147" font-size="21" font-weight="800" fill="{bright}" text-anchor="middle">{fmt_num(contributions)}</text>
  <text x="392" y="205" font-size="8" fill="{muted}" font-weight="700" text-anchor="middle">CONTRIBUTIONS</text>
  {contribution_rows}

  <!-- CARD 3: title centered; bottom split into left metric + right streak list -->
  <rect x="612" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="752" y="55" font-size="14" font-weight="700" fill="{green}" text-anchor="middle">Streak Record</text>
  <line x1="752" y1="70" x2="752" y2="214" stroke="{card}" stroke-width="1"/>
  {ring(684,140,46,record_pct,bright,7)}
  <text x="684" y="147" font-size="22" font-weight="800" fill="{bright}" text-anchor="middle">{longest}</text>
  <text x="684" y="205" font-size="8" fill="{muted}" font-weight="700" text-anchor="middle">LONGEST STREAK</text>
  {streak_rows}

  <!-- CARD 4: languages -->
  <rect x="28" y="258" width="864" height="160" rx="12" fill="{card}" stroke="{border}"/>
  <text x="46" y="285" font-size="14" font-weight="700" fill="{green}">Most Used Languages</text>
  <text x="874" y="285" font-size="8" fill="{muted}" text-anchor="end">Repository language bytes</text>
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
        calendar_commits,
        calendar_prs,
        calendar_issues,
        reviews,
        repo_contributions,
        days,
        start,
        end,
    ) = contribution_data()

    # The profile calendar total is authoritative. Component counts come from
    # the same GitHub contribution collection when available.
    if calendar_commits is None:
        commits = commit_total()
        prs = search_total(f"author:{OWNER} type:pr")
        issues = search_total(f"author:{OWNER} type:issue")
    else:
        commits = calendar_commits
        prs = calendar_prs
        issues = calendar_issues

    current, longest, active_days, best_day = streaks(days)
    langs = language_percentages(repos)

    benchmarks = benchmark_profile_stats()
    rank, rank_score, rank_ceilings = rank_developer(
        stars, commits, prs, issues, repo_count, benchmarks
    )

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    svg = build_svg(
        repo_count, stars, commits, prs, issues, contributions, current, longest,
        calendar_prs, calendar_issues, reviews, repo_contributions,
        active_days, best_day, langs, updated, rank
    )

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
        "active_days": active_days,
        "best_day": best_day,
        "period": f"{start}..{end}",
        "metric_scope": "GitHub contribution collection for the last year",
        "developer_rank": rank,
        "rank_score": rank_score,
        "rank_benchmark_users": list(RANK_BENCHMARK_USERS),
        "rank_benchmark_ceilings": rank_ceilings,
    }, indent=2))

if __name__ == "__main__":
    main()
