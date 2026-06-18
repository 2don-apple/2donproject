import os
import re
import json
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup


CONFIG_SECRET_NAME = "PUBG_ALERT_CONFIG_JSON"
CONFIG_JSON = os.getenv(CONFIG_SECRET_NAME, "").strip()

STATE_FILE = Path("data/pubg_sent.json")

PUBG_NEWS_URL = "https://pubg.com/en/news"
PUBG_EVENT_URLS = [
    "https://pubg.com/en/events/news",
    "https://pubg.com/en/events",
]
PUBG_MAP_REPORT_FALLBACK_URL = "https://pubg.com/en/news/10181"

KST = timezone(timedelta(hours=9))

MAP_KO = {
    "Erangel": "에란겔",
    "Taego": "태이고",
    "Miramar": "미라마",
    "Sanhok": "사녹",
    "Vikendi": "비켄디",
    "Karakin": "카라킨",
    "Paramo": "파라모",
    "Rondo": "론도",
    "Deston": "데스턴",
    "Haven": "헤이븐",
}


def now_kst() -> datetime:
    return datetime.now(KST)


def ensure_state_file():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text("{}", encoding="utf-8")


def load_state() -> dict:
    ensure_state_file()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    if not CONFIG_JSON:
        raise RuntimeError(f"{CONFIG_SECRET_NAME} Secret이 비어있습니다.")
    data = json.loads(CONFIG_JSON)
    if not isinstance(data, dict):
        raise RuntimeError("PUBG alert config JSON 형식이 올바르지 않습니다.")
    data.setdefault("guilds", {})
    return data


def fetch_html(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": "DoniBot PUBG Alert/1.0 (+https://github.com/2don-apple/2donproject)",
        "Accept-Language": "en-US,en;q=0.9,ko-KR;q=0.8,ko;q=0.7",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def clean_text(s: str) -> str:
    s = html.unescape(str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def soup_text_lines(raw_html: str) -> list[str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = []
    for line in text.splitlines():
        t = clean_text(line)
        if t:
            lines.append(t)
    return lines


def abs_pubg_url(href: str) -> str:
    href = str(href or "").strip()
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://pubg.com" + href
    return "https://pubg.com/" + href


def article_id_from_url(url: str) -> str:
    m = re.search(r"/news/(\d+)", url)
    if m:
        return m.group(1)
    return re.sub(r"\W+", "_", url).strip("_")


def parse_article(url: str) -> dict | None:
    try:
        raw = fetch_html(url)
    except Exception as e:
        print(f"[WARN] article fetch failed url={url} err={type(e).__name__}: {e}")
        return None

    lines = soup_text_lines(raw)
    if not lines:
        return None

    title = ""
    date = ""
    category = ""

    for i, line in enumerate(lines):
        if line.upper() in ("PC", "CONSOLE"):
            continue

        if re.search(r"\d{4}\.\d{2}\.\d{2}", line):
            date = re.search(r"\d{4}\.\d{2}\.\d{2}", line).group(0)
            if i >= 1:
                category = lines[i - 1]
            break

    # 제목 후보: 날짜 앞쪽의 가장 기사 제목 같은 줄
    for i, line in enumerate(lines[:80]):
        if line in ("ENGLISH (GLOBAL)", "PLAY NOW", "GO BACK TO LIST", "PC", "CONSOLE"):
            continue
        if line.startswith("Image:"):
            continue
        if re.search(r"\d{4}\.\d{2}\.\d{2}", line):
            break
        if len(line) >= 4:
            title = line
            # 페이지 제목 이후 더 나은 제목이 나오면 갱신
            if " - NEWS" not in line:
                pass

    # 실제 페이지 구조에서 ### 제목이 텍스트로 들어오는 경우 보정
    for line in lines[:80]:
        if line in ("PC", "CONSOLE", "GO BACK TO LIST"):
            continue
        if line.upper() in ("ANNOUNCEMENT", "PATCH NOTES", "DEV LETTER", "ESPORTS"):
            continue
        if re.search(r"\d{4}\.\d{2}\.\d{2}", line):
            continue
        if "PUBG BATTLEGROUNDS" in line:
            continue
        if len(line) >= 4 and not line.startswith("Image:"):
            title = line
            break

    full_text = "\n".join(lines)

    platform_pc = "\nPC\n" in "\n" + full_text + "\n" or " PC " in full_text
    platform_console = "\nCONSOLE\n" in "\n" + full_text + "\n" or " CONSOLE " in full_text

    # ✅ Steam / PC 기준
    # - PC 태그가 있거나, 콘솔 전용이 아니면 허용
    # - Console only는 제외
    if platform_console and not platform_pc:
        return None

    # ✅ KAKAO 전용 제외
    if "KAKAO" in full_text and not platform_pc:
        return None

    desc = ""
    for line in lines[80:180]:
        if line.startswith("Image:"):
            continue
        if line.upper() in ("PREV", "NEXT"):
            break
        if len(line) >= 20:
            desc = line
            break

    return {
        "id": article_id_from_url(url),
        "title": clean_text(title),
        "date": date,
        "category": clean_text(category),
        "url": url,
        "description": clean_text(desc),
    }


def collect_article_urls_from_page(url: str, limit: int = 12) -> list[str]:
    try:
        raw = fetch_html(url)
    except Exception as e:
        print(f"[WARN] list fetch failed url={url} err={type(e).__name__}: {e}")
        return []

    soup = BeautifulSoup(raw, "html.parser")
    urls = []

    for a in soup.find_all("a", href=True):
        href = abs_pubg_url(a.get("href"))
        if not re.search(r"/(?:en/)?news/\d+", href):
            continue
        if href not in urls:
            urls.append(href)

    # HTML 안에 직접 들어있는 링크도 추가
    for m in re.finditer(r'href=["\']([^"\']*/news/\d+[^"\']*)["\']', raw):
        href = abs_pubg_url(m.group(1))
        if href not in urls:
            urls.append(href)

    return urls[:limit]


def get_latest_articles(kind: str, limit: int = 3) -> list[dict]:
    if kind == "event":
        pages = PUBG_EVENT_URLS + [
            "https://pubg.com/en/news?category=event",
            "https://pubg.com/en/news",
        ]
        event_keywords = ("event", "challenge", "reward", "mission", "pass", "pnc", "fantasy")
    else:
        pages = [
            "https://pubg.com/en/news",
            "https://pubg.com/en/news?category=announcement",
            "https://pubg.com/en/news?category=notice",
        ]
        event_keywords = ()

    urls = []
    for page in pages:
        for u in collect_article_urls_from_page(page, limit=16):
            if u not in urls:
                urls.append(u)

    articles = []
    for url in urls:
        article = parse_article(url)
        if not article:
            continue

        title_l = article["title"].lower()
        cat_l = article["category"].lower()

        if kind == "event":
            if not any(k in title_l or k in cat_l for k in event_keywords):
                continue
        else:
            # 공지사항은 이벤트성 글은 너무 많이 섞이지 않게 약하게 제외
            if "esports" in cat_l:
                continue

        articles.append(article)
        if len(articles) >= limit:
            break

    return articles[:limit]


def find_latest_map_report_url() -> str:
    urls = collect_article_urls_from_page("https://pubg.com/en/news", limit=30)
    candidates = []

    for url in urls:
        try:
            article = parse_article(url)
        except Exception:
            article = None

        if not article:
            continue

        title = article.get("title", "")
        if "Map Service Report" in title:
            candidates.append(url)

    if candidates:
        return candidates[0]

    return PUBG_MAP_REPORT_FALLBACK_URL


def lines_between(lines: list[str], start_pat: str, end_pats: tuple[str, ...]) -> list[str]:
    start = -1
    for i, line in enumerate(lines):
        if re.search(start_pat, line, re.I):
            start = i
            break

    if start < 0:
        return []

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if any(re.search(p, lines[j], re.I) for p in end_pats):
            end = j
            break

    return lines[start:end]


def parse_schedule(lines: list[str]) -> list[dict]:
    block = lines_between(lines, r"^Schedule$", (r"^Normal Match$",))
    result = []

    # 공식 페이지 텍스트 구조:
    # Week 1 / June 17 / June 25 / Week 2 / June 24 ...
    for i, line in enumerate(block):
        m = re.fullmatch(r"Week\s+(\d+)", line, re.I)
        if not m:
            continue

        week = int(m.group(1))
        pc_date = ""
        for j in range(i + 1, min(i + 5, len(block))):
            if re.fullmatch(r"[A-Za-z]+\s+\d{1,2}", block[j]):
                pc_date = block[j]
                break

        if pc_date:
            result.append({"week": week, "pc_date": pc_date})

    return result


def parse_normal_as_maps(lines: list[str]) -> dict[int, list[str]]:
    normal_block = lines_between(lines, r"^Normal Match$", (r"^Ranked$",))
    as_block = lines_between(normal_block, r"^AS$", (r"^SEA$", r"^KAKAO$", r"^NA$", r"^SA$", r"^EU$", r"^RU$", r"^Console"))

    result = {}

    for i, line in enumerate(as_block):
        m = re.fullmatch(r"Week\s+(\d+)", line, re.I)
        if not m:
            continue

        week = int(m.group(1))
        maps = []

        for t in as_block[i + 1:]:
            if re.fullmatch(r"Week\s+\d+", t, re.I):
                break
            if t in ("Fixed", "Favored", "Etc."):
                continue
            if t in MAP_KO:
                maps.append(MAP_KO[t])

        if maps:
            result[week] = maps[:5]

    return result


def parse_ranked_maps(lines: list[str]) -> list[str]:
    ranked_block = lines_between(lines, r"^Ranked$", (r"We’ll see you", r"PUBG: BATTLEGROUNDS Team", r"^PREV$", r"^NEXT$"))
    text = " ".join(ranked_block)

    maps = []
    for name in MAP_KO:
        if re.search(rf"\b{re.escape(name)}\b", text):
            maps.append(MAP_KO[name])

    # 공식 표 순서 보정
    order = ["에란겔", "미라마", "태이고", "론도", "비켄디", "데스턴", "사녹", "카라킨", "파라모"]
    maps = [m for m in order if m in maps]

    return maps


def parse_month_day(s: str, year: int) -> datetime:
    dt = datetime.strptime(f"{year} {s}", "%Y %B %d")
    return dt.replace(tzinfo=KST)


def current_week_from_schedule(schedule: list[dict], ref: datetime) -> tuple[int, datetime, datetime] | None:
    if not schedule:
        return None

    year = ref.year
    starts = []

    for item in schedule:
        try:
            start = parse_month_day(item["pc_date"], year)
        except Exception:
            continue

        starts.append((item["week"], start))

    starts.sort(key=lambda x: x[1])

    if not starts:
        return None

    chosen = starts[0]
    end = starts[1][1] if len(starts) >= 2 else starts[0][1] + timedelta(days=7)

    for idx, pair in enumerate(starts):
        week, start = pair
        next_start = starts[idx + 1][1] if idx + 1 < len(starts) else start + timedelta(days=7)

        if start.date() <= ref.date() < next_start.date():
            chosen = pair
            end = next_start
            break

    return chosen[0], chosen[1], end


def fallback_map_rotation(guild_cfg: dict) -> dict:
    normal = guild_cfg.get("normal_maps") or guild_cfg.get("map_rotation") or ["에란겔", "태이고", "미라마", "사녹", "비켄디"]
    ranked = guild_cfg.get("ranked_maps") or ["에란겔", "미라마", "태이고", "론도"]

    today = now_kst()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=7)

    return {
        "week": 0,
        "start": start,
        "end": end,
        "normal": normal,
        "ranked": ranked,
        "source_url": guild_cfg.get("map_report_url") or PUBG_MAP_REPORT_FALLBACK_URL,
        "fallback": True,
    }


def get_current_map_rotation(guild_cfg: dict) -> dict:
    url = guild_cfg.get("map_report_url") or find_latest_map_report_url()

    try:
        raw = fetch_html(url)
        lines = soup_text_lines(raw)

        schedule = parse_schedule(lines)
        normal_by_week = parse_normal_as_maps(lines)
        ranked = parse_ranked_maps(lines)

        picked = current_week_from_schedule(schedule, now_kst())
        if not picked:
            raise RuntimeError("현재 주차를 찾지 못했습니다.")

        week, start, end = picked
        normal = normal_by_week.get(week) or guild_cfg.get("normal_maps")

        if not normal:
            raise RuntimeError("AS 일반전 맵을 찾지 못했습니다.")

        if not ranked:
            ranked = guild_cfg.get("ranked_maps") or ["에란겔", "미라마", "태이고", "론도"]

        return {
            "week": week,
            "start": start,
            "end": end,
            "normal": normal,
            "ranked": ranked,
            "source_url": url,
            "fallback": False,
        }

    except Exception as e:
        print(f"[WARN] map rotation parse failed: {type(e).__name__}: {e}")
        return fallback_map_rotation(guild_cfg)


def format_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y.%m.%d")


def discord_post(webhook_url: str, content: str):
    r = requests.post(
        webhook_url,
        json={
            "content": content,
            "allowed_mentions": {"parse": []},
        },
        timeout=20,
    )
    r.raise_for_status()


def should_send(state: dict, key: str) -> bool:
    return not bool(state.get(key))


def mark_sent(state: dict, key: str):
    state[key] = now_kst().isoformat()


def send_map_rotation_for_guild(gid: str, guild_cfg: dict, state: dict):
    data = get_current_map_rotation(guild_cfg)

    key = f"{gid}:map_rotation:{format_date(data['start'])}:{format_date(data['end'])}"
    if not should_send(state, key):
        print(f"[SKIP] map already sent gid={gid} key={key}")
        return

    normal_text = " / ".join(data["normal"])
    ranked_text = " / ".join(data["ranked"])

    content = (
        "🗺️ PUBG 맵 로테이션 안내\n\n"
        "기준: Steam / PC / AS 서버\n"
        f"기간: {format_date(data['start'])} ~ {format_date(data['end'])}\n\n"
        "이번 주 일반전 맵:\n"
        f"{normal_text}\n"
        "이번주 경쟁전 맵:\n"
        f"{ranked_text}"
    )

    discord_post(guild_cfg["webhook_url"], content)
    mark_sent(state, key)

    print(f"[SENT] map rotation gid={gid} week={data['week']} fallback={data['fallback']}")


def send_articles_for_guild(gid: str, guild_cfg: dict, kind: str, state: dict):
    articles = get_latest_articles(kind, limit=3)

    label = "공지사항" if kind == "notice" else "이벤트"
    emoji = "📢" if kind == "notice" else "🎁"

    for article in articles:
        key = f"{gid}:{kind}:{article['id']}"
        if not should_send(state, key):
            print(f"[SKIP] {kind} already sent gid={gid} id={article['id']}")
            continue

        desc = article.get("description") or ""
        if len(desc) > 180:
            desc = desc[:177] + "..."

        content = (
            f"{emoji} PUBG {label} 안내\n\n"
            "기준: Steam / PC / AS 서버\n"
            f"제목: {article['title']}\n"
            f"날짜: {article.get('date') or '-'}\n"
            f"링크: {article['url']}"
        )

        if desc:
            content += f"\n\n{desc}"

        discord_post(guild_cfg["webhook_url"], content)
        mark_sent(state, key)

        print(f"[SENT] {kind} gid={gid} id={article['id']} title={article['title']}")


def main():
    config = load_config()
    state = load_state()

    guilds = config.get("guilds") or {}

    for gid, guild_cfg in guilds.items():
        if not isinstance(guild_cfg, dict):
            continue

        if not guild_cfg.get("enabled"):
            continue

        webhook_url = str(guild_cfg.get("webhook_url") or "").strip()
        if not webhook_url:
            continue

        # ✅ Steam / PC / AS 서버 기준 강제
        guild_cfg["platform"] = "steam"
        guild_cfg["device"] = "pc"
        guild_cfg["server_region"] = "as"

        types = guild_cfg.get("types") or {}

        if types.get("map_rotation"):
            send_map_rotation_for_guild(str(gid), guild_cfg, state)

        if types.get("notice"):
            send_articles_for_guild(str(gid), guild_cfg, "notice", state)

        if types.get("event"):
            send_articles_for_guild(str(gid), guild_cfg, "event", state)

    save_state(state)


if __name__ == "__main__":
    main()
