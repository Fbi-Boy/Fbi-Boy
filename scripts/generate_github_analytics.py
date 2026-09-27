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
OUT = Path("assets/github-analytics-v23.svg")
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
    if not total_bytes:
        return []

    top = ranked[:5]
    remainder = sum(count for _, count in ranked[5:])
    rows = [(name, safe_percent(count, total_bytes)) for name, count in top]

    # Keep the card stable when new languages are detected: everything outside
    # the top five is automatically grouped into "Others".
    if remainder:
        rows.append(("Others", safe_percent(remainder, total_bytes)))
    return rows

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
              active_days, best_day, langs, updated, rank, days):
    W, H = 920, 510
    bg = "#0b0f14"
    card = "#121820"
    border = "#4b5563"
    text = "#d5dbe3"
    green = "#9ca3af"
    bright = "#e5e7eb"
    muted = "#8f99a8"
    track = "#202833"

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

    # Trading-style contribution line: aggregate the last 365 days into 52 weekly points.
    weekly = []
    if days:
        ordered = sorted(days, key=lambda item: item["date"])
        bucket = []
        week_start = ordered[0]["date"]
        for item in ordered:
            if (item["date"] - week_start).days >= 7:
                weekly.append(sum(bucket))
                bucket = []
                week_start = item["date"]
            bucket.append(int(item["count"]))
        if bucket:
            weekly.append(sum(bucket))
    weekly = weekly[-52:] or [0]

    chart_x, chart_y, chart_w, chart_h = 338, 94, 246, 112
    max_week = max(weekly) or 1
    min_week = min(weekly)
    span = max(1, max_week - min_week)
    points = []
    for i, value in enumerate(weekly):
        px = chart_x + (chart_w * i / max(1, len(weekly) - 1))
        py = chart_y + chart_h - ((value - min_week) / span) * chart_h
        points.append((px, py))

    polyline = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
    area = f"{chart_x},{chart_y+chart_h} " + polyline + f" {chart_x+chart_w},{chart_y+chart_h}"
    grid = "".join(
        f'<line x1="{chart_x}" y1="{chart_y + chart_h*i/4:.1f}" '
        f'x2="{chart_x+chart_w}" y2="{chart_y + chart_h*i/4:.1f}" '
        f'stroke="#202833" stroke-width="1"/>'
        for i in range(5)
    )
    last_value = weekly[-1]
    last_x, last_y = points[-1]
    contribution_chart = f'''
      {grid}
      <polygon points="{area}" fill="{green}" opacity="0.08"/>
      <polyline points="{polyline}" fill="none" stroke="{green}" stroke-width="2.4"
                stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" fill="{card}" stroke="{bright}" stroke-width="2"/>
      <text x="{chart_x}" y="{chart_y-7}" font-size="7.5" fill="{muted}">WEEKLY CONTRIBUTIONS</text>
      <text x="{chart_x+chart_w}" y="{chart_y-7}" font-size="7.5" fill="{bright}" text-anchor="end">{fmt_num(last_value)} LAST</text>
      <text x="{chart_x}" y="{chart_y+chart_h+14}" font-size="7" fill="{muted}">-52W</text>
      <text x="{chart_x+chart_w}" y="{chart_y+chart_h+14}" font-size="7" fill="{muted}" text-anchor="end">NOW</text>
    '''

    streak_rows = list_rows([
        ("Current", f"{current} d"),
        ("Longest", f"{longest} d"),
        ("Active", f"{active_days} d"),
        ("Best Day", fmt_num(best_day)),
        ("Contributions", fmt_num(contributions)),
    ], 754, 810, 876)

    lang_rows = []
    # Language accents follow each language's recognizable brand/logo color.
    language_icon_paths = {
        "laravel": "M23.642 5.43a.364.364 0 01.014.1v5.149c0 .135-.073.26-.189.326l-4.323 2.49v4.934a.378.378 0 01-.188.326L9.93 23.949a.316.316 0 01-.066.027c-.008.002-.016.008-.024.01a.348.348 0 01-.192 0c-.011-.002-.02-.008-.03-.012-.02-.008-.042-.014-.062-.025L.533 18.755a.376.376 0 01-.189-.326V2.974c0-.033.005-.066.014-.098.003-.012.01-.02.014-.032.007-.02.015-.04.023-.058.004-.013.015-.022.023-.033l.033-.045c.012-.01.025-.018.037-.027.014-.012.027-.024.041-.034H.53L5.043.05a.375.375 0 01.375 0L9.93 2.647h.002c.015.01.027.021.04.033l.038.027c.013.014.02.03.033.045.008.011.02.021.025.033.01.02.017.038.024.058.003.011.01.021.013.032.01.031.014.064.014.098v9.652l3.76-2.164V5.527c0-.033.004-.066.013-.098.003-.01.01-.02.013-.032a.487.487 0 01.024-.059c.007-.012.018-.02.025-.033.012-.015.021-.03.033-.043.012-.012.025-.02.037-.028.014-.01.026-.023.041-.032h.001l4.513-2.598a.375.375 0 01.375 0l4.513 2.598c.016.01.027.021.042.031.012.01.025.018.036.028.013.014.022.03.034.044.008.012.019.021.024.033.011.02.018.04.024.06.006.01.012.021.015.032zm-.74 5.032V6.179l-1.578.908-2.182 1.256v4.283zm-4.51 7.75v-4.287l-2.147 1.225-6.126 3.498v4.325zM1.093 3.624v14.588l8.273 4.761v-4.325l-4.322-2.445-.002-.003H5.04c-.014-.01-.025-.021-.04-.031-.011-.01-.024-.018-.035-.027l-.001-.002c-.013-.012-.021-.025-.031-.04-.01-.011-.021-.022-.028-.036h-.002c-.008-.014-.013-.031-.02-.047-.006-.016-.014-.027-.018-.043a.49.49 0 01-.008-.057c-.002-.014-.006-.027-.006-.041V5.789l-2.18-1.257zm4.137-2.814L1.47 2.974l3.76 2.164 3.758-2.164zm1.956 13.505l2.182-1.256V3.624l-1.58.91-2.182 1.255v9.435zm11.581-10.95l-3.76 2.163 3.76 2.163 3.759-2.164zm-.376 4.978l-2.181-1.256L16.21 7.087 14.63 6.18v4.283l2.182 1.256 1.58.908zm-8.65 9.654l5.514-3.148 2.756-1.572-3.757-2.163-4.323 2.489-3.941 2.27z",
        "php": "M7.01 10.207h-.944l-.515 2.648h.838c.556 0 .97-.105 1.242-.314.272-.21.455-.559.55-1.049.092-.47.05-.802-.124-.995-.175-.193-.523-.29-1.047-.29zM12 5.688C5.373 5.688 0 8.514 0 12s5.373 6.313 12 6.313S24 15.486 24 12c0-3.486-5.373-6.312-12-6.312zm-3.26 7.451c-.261.25-.575.438-.917.551-.336.108-.765.164-1.285.164H5.357l-.327 1.681H3.652l1.23-6.326h2.65c.797 0 1.378.209 1.744.628.366.418.476 1.002.33 1.752a2.836 2.836 0 01-.305.847c-.143.255-.33.49-.561.703zm4.024.715l.543-2.799c.063-.318.039-.536-.068-.651-.107-.116-.336-.174-.687-.174H11.46l-.704 3.625H9.388l1.23-6.327h1.367l-.327 1.682h1.218c.767 0 1.295.134 1.586.401s.378.7.263 1.299l-.572 2.944h-1.389zm7.597-2.265a2.782 2.782 0 01-.305.847c-.143.255-.33.49-.561.703a2.44 2.44 0 01-.917.551c-.336.108-.765.164-1.286.164h-1.18l-.327 1.682h-1.378l1.23-6.326h2.649c.797 0 1.378.209 1.744.628.366.417.477 1.001.331 1.751zM17.766 10.207h-.943l-.516 2.648h.838c.557 0 .971-.105 1.242-.314.272-.21.455-.559.551-1.049.092-.47.049-.802-.125-.995s-.524-.29-1.047-.29z",
        "python": "M14.25.18l.9.2.73.26.59.3.45.32.34.34.25.34.16.33.1.3.04.26.02.2-.01.13V8.5l-.05.63-.13.55-.21.46-.26.38-.3.31-.33.25-.35.19-.35.14-.33.1-.3.07-.26.04-.21.02H8.77l-.69.05-.59.14-.5.22-.41.27-.33.32-.27.35-.2.36-.15.37-.1.35-.07.32-.04.27-.02.21v3.06H3.17l-.21-.03-.28-.07-.32-.12-.35-.18-.36-.26-.36-.36-.35-.46-.32-.59-.28-.73-.21-.88-.14-1.05-.05-1.23.06-1.22.16-1.04.24-.87.32-.71.36-.57.4-.44.42-.33.42-.24.4-.16.36-.1.32-.05.24-.01h.16l.06.01h8.16v-.83H6.18l-.01-2.75-.02-.37.05-.34.11-.31.17-.28.25-.26.31-.23.38-.2.44-.18.51-.15.58-.12.64-.1.71-.06.77-.04.84-.02 1.27.05zm-6.3 1.98l-.23.33-.08.41.08.41.23.34.33.22.41.09.41-.09.33-.22.23-.34.08-.41-.08-.41-.23-.33-.33-.22-.41-.09-.41.09zm13.09 3.95l.28.06.32.12.35.18.36.27.36.35.35.47.32.59.28.73.21.88.14 1.04.05 1.23-.06 1.23-.16 1.04-.24.86-.32.71-.36.57-.4.45-.42.33-.42.24-.4.16-.36.09-.32.05-.24.02-.16-.01h-8.22v.82h5.84l.01 2.76.02.36-.05.34-.11.31-.17.29-.25.25-.31.24-.38.2-.44.2-.51.15-.58.13-.64.09-.71.07-.77.04-.84.01-1.27-.04-1.07-.14-.9-.2-.73-.25-.59-.3-.45-.33-.34-.34-.25-.34-.16-.33-.1-.3-.04-.25-.02-.2.01-.13v-5.34l.05-.64.13-.54.21-.46.26-.38.3-.32.33-.24.35-.2.35-.14.33-.1.3-.06.26-.04.21-.02.13-.01h5.84l.69-.05.59-.14.5-.21.41-.28.33-.32.27-.35.2-.36.15-.36.1-.35.07-.32.04-.28.02-.21V6.07h2.09l.14.01zm-6.47 14.25l-.23.33-.08.41.08.41.23.33.33.23.41.08.41-.08.33-.23.23-.33.08-.41-.08-.41-.23-.33-.33-.23-.41-.08-.41.08z",
        "dart": "M4.105 4.105S9.158 1.58 11.684.316a3.079 3.079 0 011.481-.315c.766.047 1.677.788 1.677.788L24 9.948v9.789h-4.263V24H9.789l-9-9C.303 14.5 0 13.795 0 13.105c0-.319.18-.818.316-1.105l3.789-7.895zm.679.679v11.787c.002.543.021 1.024.498 1.508L10.204 23h8.533v-4.263L4.784 4.784zm12.055-.678c-.899-.896-1.809-1.78-2.74-2.643-.302-.267-.567-.468-1.07-.462-.37.014-.87.195-.87.195L6.341 4.105l10.498.001z",
        "javascript": "M0 0h24v24H0V0zm22.034 18.276c-.175-1.095-.888-2.015-3.003-2.873-.736-.345-1.554-.585-1.797-1.14-.091-.33-.105-.51-.046-.705.15-.646.915-.84 1.515-.66.39.12.75.42.976.9 1.034-.676 1.034-.676 1.755-1.125-.27-.42-.404-.601-.586-.78-.63-.705-1.469-1.065-2.834-1.034l-.705.089c-.676.165-1.32.525-1.71 1.005-1.14 1.291-.811 3.541.569 4.471 1.365 1.02 3.361 1.244 3.616 2.205.24 1.17-.87 1.545-1.966 1.41-.811-.18-1.26-.586-1.755-1.336l-1.83 1.051c.21.48.45.689.81 1.109 1.74 1.756 6.09 1.666 6.871-1.004.029-.09.24-.705.074-1.65l.046.067zm-8.983-7.245h-2.248c0 1.938-.009 3.864-.009 5.805 0 1.232.063 2.363-.138 2.711-.33.689-1.18.601-1.566.48-.396-.196-.597-.466-.83-.855-.063-.105-.11-.196-.127-.196l-1.825 1.125c.305.63.75 1.172 1.324 1.517.855.51 2.004.675 3.207.405.783-.226 1.458-.691 1.811-1.411.51-.93.402-2.07.397-3.346.012-2.054 0-4.109 0-6.179l.004-.056z"
}
    language_styles = {
        "Blade": ("#FF2D20", "laravel"),
        "PHP": ("#777BB4", "php"),
        "Python": ("#3776AB", "python"),
        "Dart": ("#0175C2", "dart"),
        "JavaScript": ("#F7DF1E", "javascript"),
        "TypeScript": ("#3178C6", "typescript"),
        "C++": ("#00599C", "cplusplus"),
        "C": ("#A8B9CC", "c"),
        "Java": ("#ED8B00", "openjdk"),
        "Go": ("#00ADD8", "go"),
        "Rust": ("#DEA584", "rust"),
        "Ruby": ("#CC342D", "ruby"),
        "Kotlin": ("#7F52FF", "kotlin"),
        "HTML": ("#E34F26", "html5"),
        "CSS": ("#1572B6", "css3"),
        "SQL": ("#4479A1", "mysql"),
        "Shell": ("#89E051", "gnubash"),
    }
    y = 326
    for idx, (name, pct) in enumerate(langs[:6], start=1):
        accent, icon_name = language_styles.get(name, ("#9CA3AF", "code"))
        rank_label = f"{idx:02d}"
        bar_width = 500 * pct / 100
        row_y = y - 13
        icon_path = language_icon_paths.get(icon_name, "")
        logo_svg = (f'<g transform="translate(98 {row_y+5}) scale(0.75)"><path d="{icon_path}" fill="{accent}"/></g>' if icon_path else f'<circle cx="103" cy="{y-2}" r="2.2" fill="{accent}"/><circle cx="107" cy="{y-2}" r="2.2" fill="{accent}"/><circle cx="111" cy="{y-2}" r="2.2" fill="{accent}"/>')
        lang_rows.append(
            f'<rect x="46" y="{row_y}" width="828" height="24" rx="8" fill="{track}"/>'
            f'<rect x="54" y="{row_y+3}" width="30" height="18" rx="6" fill="#252c36"/>'
            f'<text x="69" y="{y+2}" font-size="7.5" font-weight="800" fill="#c7cdd5" text-anchor="middle">{rank_label}</text>'
            f'<rect x="92" y="{row_y+3}" width="30" height="18" rx="6" fill="#171d26" stroke="#3f4854" stroke-width="1"/>'
            logo_svg
            f'<text x="136" y="{y+2}" font-size="10" font-weight="700" fill="{text}">{esc(name)}</text>'
            f'<rect x="260" y="{y-6}" width="500" height="8" rx="4" fill="{bg}"/>'
            f'<rect x="260" y="{y-6}" width="{max(3, bar_width):.1f}" height="8" rx="4" fill="{accent}"/>'
            f'<text x="846" y="{y+2}" font-size="10.5" font-weight="800" fill="{accent}" text-anchor="end">{pct:.1f}%</text>'
        )
        y += 27


    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect width="{W}" height="510" rx="18" fill="{bg}"/>
<rect x="12" y="12" width="896" height="486" rx="18" fill="{bg}" stroke="{border}"/>

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

  <!-- CARD 2: trading-style contribution line chart -->
  <rect x="320" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="460" y="55" font-size="14" font-weight="700" fill="{green}" text-anchor="middle">Contribution Activity</text>
  <text x="336" y="78" font-size="18" font-weight="800" fill="{bright}">{fmt_num(contributions)}</text>
  <text x="336" y="88" font-size="7" fill="{muted}">TOTAL • LAST 365 DAYS</text>
  {contribution_chart}

  <!-- CARD 3: title centered; bottom split into left metric + right streak list -->
  <rect x="612" y="28" width="280" height="214" rx="12" fill="{card}" stroke="{border}"/>
  <text x="752" y="55" font-size="14" font-weight="700" fill="{green}" text-anchor="middle">Streak Record</text>
  <line x1="752" y1="70" x2="752" y2="214" stroke="{card}" stroke-width="1"/>
  {ring(684,140,46,record_pct,bright,7)}
  <text x="684" y="147" font-size="22" font-weight="800" fill="{bright}" text-anchor="middle">{longest}</text>
  <text x="684" y="205" font-size="8" fill="{muted}" font-weight="700" text-anchor="middle">LONGEST STREAK</text>
  {streak_rows}

  <!-- CARD 4: dynamic language ranking -->
  <rect x="28" y="258" width="864" height="230" rx="12" fill="{card}" stroke="{border}"/>
  <rect x="46" y="271" width="28" height="24" rx="7" fill="#202631" stroke="#4b5563"/>
  <text x="60" y="288" font-size="12" font-weight="800" fill="#d6dbe3" text-anchor="middle">&lt;/&gt;</text>
  <text x="86" y="285" font-size="14" font-weight="700" fill="#e1e5ea">Most Used Languages</text>
  <text x="874" y="285" font-size="8" font-weight="700" fill="#8f99a8" text-anchor="end">AUTO • TOP 5 + OTHERS</text>
  {''.join(lang_rows)}
</g>

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
        active_days, best_day, langs, updated, rank, days
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
