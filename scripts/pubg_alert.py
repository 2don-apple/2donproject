import os
import re
import json
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))


def _parse_date_yyyy_mm_dd(value: str):
    value = str(value or "").strip()
    if not value:
        return None

    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_post_date_to_date(value: str):
    """
    PUBG 게시글 날짜를 date로 변환.
    지원 예:
    - 2026-06-28
    - 2026.06.28
    - 2026/06/28
    - 2026-06-28T01:23:45Z
    """
    s = str(value or "").strip()
    if not s:
        return None

    s = s.replace(".", "-").replace("/", "-")

    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_datetime_iso(value: str):
    """
    ISO datetime 변환.
    지원 예:
    - 2026-07-08T01:23:45Z
    - 2026-07-08T01:23:45+00:00
    """
    s = str(value or "").strip()
    if not s:
        return None

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _is_after_guild_enabled_time(item: dict, guild_cfg: dict) -> bool:
    """
    서버가 PUBG 알림을 켠 이후 게시글만 허용.

    우선순위:
    1) 게시글에 published_at 같은 정확한 시간이 있고, enabled_since도 있으면 시간 기준 비교
    2) 시간이 없으면 enabled_since_date 기준으로 날짜 비교
    3) 게시글 날짜를 전혀 못 읽으면 신규 서버 과거글 폭탄 방지를 위해 False
    """
    if not isinstance(item, dict):
        return False

    if not isinstance(guild_cfg, dict):
        return True

    enabled_dt = _parse_datetime_iso(guild_cfg.get("enabled_since"))
    enabled_date = _parse_date_yyyy_mm_dd(guild_cfg.get("enabled_since_date"))

    # 기준값이 아예 없으면 구버전 호환으로 허용
    if not enabled_dt and not enabled_date:
        return True

    # 1) 정확한 게시 시각 비교
    post_dt = (
        _parse_datetime_iso(item.get("published_at"))
        or _parse_datetime_iso(item.get("published"))
        or _parse_datetime_iso(item.get("posted_at"))
        or _parse_datetime_iso(item.get("created_at"))
    )

    if enabled_dt and post_dt:
        try:
            if post_dt.tzinfo is None:
                post_dt = post_dt.replace(tzinfo=timezone.utc)
            if enabled_dt.tzinfo is None:
                enabled_dt = enabled_dt.replace(tzinfo=timezone.utc)
            return post_dt >= enabled_dt
        except Exception:
            pass

    # 2) 날짜 비교 fallback
    post_date = (
        _parse_post_date_to_date(item.get("published_date"))
        or _parse_post_date_to_date(item.get("date"))
        or _parse_post_date_to_date(item.get("published_at"))
        or _parse_post_date_to_date(item.get("posted_at"))
        or _parse_post_date_to_date(item.get("created_at"))
    )

    if enabled_date and post_date:
        return post_date >= enabled_date

    # 3) 날짜를 못 읽으면 신규 서버 과거글 폭탄 방지 우선
    return False


CONFIG_SECRET_NAME = "PUBG_ALERT_CONFIG_JSON"
CONFIG_JSON = os.getenv(CONFIG_SECRET_NAME, "").strip()

STATE_FILE = Path("data/pubg_sent.json")

# =========================
# ✅ PUBG 공식 한국 페이지 기준
# =========================
PUBG_HOME = "https://www.pubg.com"
PUBG_NEWS_URL = "https://www.pubg.com/ko/news"

# ✅ 사용자가 확인한 한국 공식 카테고리 링크
PUBG_KO_CATEGORY_URLS = {
    "notice": {
        "label": "공지사항",
        "emoji": "📢",
        "urls": [
            "https://www.pubg.com/ko/news?category=notice",
        ],
    },
    "patch_notes": {
        "label": "패치노트",
        "emoji": "🛠️",
        "urls": [
            "https://www.pubg.com/ko/news?category=patch_notes",
        ],
    },
    "labs": {
        "label": "LABS",
        "emoji": "🧪",
        "urls": [
            "https://www.pubg.com/ko/news?category=labs",
        ],
    },
    "dev_notes": {
        "label": "개발일지",
        "emoji": "📝",
        "urls": [
            "https://www.pubg.com/ko/news?category=dev_notes",
        ],
    },
    # 기존 설정 types.event 호환용
    # 한국 공식 이벤트 전용 카테고리 구조가 바뀔 수 있어서,
    # 우선 ko/news 전체와 events 쪽도 같이 확인한다.
    "event": {
        "label": "이벤트",
        "emoji": "🎁",
        "urls": [
            "https://www.pubg.com/ko/events/notice",
            "https://www.pubg.com/ko/events",
        ],
    },
}

# ✅ 기존 맵 서비스 리포트 fallback
PUBG_MAP_REPORT_FALLBACK_URL = "https://www.pubg.com/ko/news/10181"

# ✅ Embed 색상
PUBG_COLOR_NOTICE = 0xF2A900
PUBG_COLOR_PATCH = 0x3498DB
PUBG_COLOR_EVENT = 0x9B59B6
PUBG_COLOR_MAP = 0x2ECC71

# ✅ 웹훅 프로필 이미지
PUBG_ALERT_AVATAR_URL = "https://raw.githubusercontent.com/2don-apple/2donproject/main/pubg_alert_icon.png?v=20260620_1"

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

    # 한국어 페이지 파싱 대응
    "에란겔": "에란겔",
    "태이고": "태이고",
    "미라마": "미라마",
    "사녹": "사녹",
    "비켄디": "비켄디",
    "카라킨": "카라킨",
    "파라모": "파라모",
    "론도": "론도",
    "데스턴": "데스턴",
    "헤이븐": "헤이븐",
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
        "User-Agent": "DoniBot PUBG Alert/1.1 (+https://github.com/2don-apple/2donproject)",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
    }

    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def clean_text(s: str) -> str:
    s = html.unescape(str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def trim_text(s: str, limit: int) -> str:
    s = clean_text(s)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)].rstrip() + "..."


def soup_text_lines(raw_html: str) -> list[str]:
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

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

    return urljoin(PUBG_HOME, href)


def article_id_from_url(url: str) -> str:
    m = re.search(r"/news/(\d+)", url)
    if m:
        return m.group(1)

    return re.sub(r"\W+", "_", url).strip("_")


def extract_meta_content(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key})
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))

        tag = soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))

    return ""


def extract_article_image(soup: BeautifulSoup, page_url: str) -> str:
    # ✅ 공식 페이지 대표 이미지 우선
    img = extract_meta_content(
        soup,
        "og:image",
        "twitter:image",
        "twitter:image:src",
    )

    if img:
        return abs_pubg_url(img)

    # ✅ fallback: 본문 이미지 후보
    for tag in soup.find_all("img"):
        src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
        src = abs_pubg_url(src)

        if not src:
            continue

        low = src.lower()

        # 아이콘/로고류 제외
        if any(x in low for x in ("logo", "icon", "favicon", "sprite")):
            continue

        return src

    return ""


def detect_category_from_url(url: str) -> str:
    u = str(url or "").lower()

    if "patch_notes" in u or "patch-notes" in u:
        return "patch_notes"

    if "dev_notes" in u or "dev-notes" in u:
        return "dev_notes"

    if "labs" in u:
        return "labs"

    if "event" in u or "/events" in u:
        return "event"

    return "notice"


def category_label(category: str) -> str:
    info = PUBG_KO_CATEGORY_URLS.get(category) or PUBG_KO_CATEGORY_URLS["notice"]
    return info["label"]


def category_emoji(category: str) -> str:
    info = PUBG_KO_CATEGORY_URLS.get(category) or PUBG_KO_CATEGORY_URLS["notice"]
    return info["emoji"]


# =========================
# ✅ 알림 상위/하위 카테고리 표시 정책
# - notice / patch_notes / labs / dev_notes 는 모두 "공지사항"으로 묶음
# - 실제 세부 타입은 #패치노트, #LABS 처럼 태그로 표시
# =========================
NOTICE_GROUP_KINDS = {"notice", "patch_notes", "labs", "dev_notes"}

CATEGORY_HASHTAG = {
    "notice": "#공지사항",
    "patch_notes": "#패치노트",
    "labs": "#LABS",
    "dev_notes": "#개발일지",
    "event": "#이벤트",
}


def primary_alert_kind(category: str) -> str:
    category = str(category or "notice").strip()

    if category in NOTICE_GROUP_KINDS:
        return "notice"

    return category


def primary_category_label(category: str) -> str:
    primary = primary_alert_kind(category)

    if primary == "notice":
        return "공지사항"

    return category_label(primary)


def secondary_category_label(category: str) -> str:
    return category_label(category)


def secondary_category_hashtag(category: str) -> str:
    category = str(category or "notice").strip()
    return CATEGORY_HASHTAG.get(category, f"#{category_label(category)}")


def category_color(category: str) -> int:
    primary = primary_alert_kind(category)

    if primary == "event":
        return PUBG_COLOR_EVENT

    if primary == "notice":
        return PUBG_COLOR_NOTICE

    return PUBG_COLOR_NOTICE


def category_main_emoji(category: str) -> str:
    primary = primary_alert_kind(category)

    if primary == "event":
        return "🎁"

    if primary == "notice":
        return "📢"

    return category_emoji(category)


def parse_article(url: str, forced_category: str = "") -> dict | None:
    try:
        raw = fetch_html(url)
    except Exception as e:
        print(f"[WARN] article fetch failed url={url} err={type(e).__name__}: {e}")
        return None

    soup = BeautifulSoup(raw, "html.parser")
    lines = soup_text_lines(raw)

    if not lines:
        return None

    meta_title = extract_meta_content(soup, "og:title", "twitter:title")
    meta_desc = extract_meta_content(soup, "og:description", "twitter:description", "description")
    image_url = extract_article_image(soup, url)

    title = ""
    date = ""
    category = forced_category or detect_category_from_url(url)

    # ✅ 날짜 탐색: 2026.06.20 / 2026-06-20 모두 대응
    for line in lines[:120]:
        m = re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", line)
        if m:
            date = m.group(0).replace("-", ".").replace("/", ".")
            break

    # ✅ 제목은 meta title 우선
    if meta_title:
        title = meta_title
        title = re.sub(r"\s*\|\s*PUBG.*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s*-\s*PUBG.*$", "", title, flags=re.I).strip()

    # ✅ meta title이 이상하면 본문 후보에서 추출
    if not title or len(title) < 4:
        skip_words = {
            "PUBG: BATTLEGROUNDS",
            "PUBG",
            "뉴스",
            "공지사항",
            "패치노트",
            "개발일지",
            "이벤트",
            "LABS",
            "PLAY NOW",
            "GO BACK TO LIST",
            "PC",
            "CONSOLE",
            "KRAFTON",
        }

        for line in lines[:100]:
            if line in skip_words:
                continue
            if re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", line):
                continue
            if len(line) >= 4 and not line.startswith("Image:"):
                title = line
                break

    full_text = "\n".join(lines)

    # ✅ PC / Console 필터
    # - Console 전용 글은 제외
    # - PC 태그가 있거나 플랫폼 태그가 애매하면 허용
    platform_pc = bool(re.search(r"(^|\n|\s)PC($|\n|\s)", full_text, re.I))
    platform_console = bool(re.search(r"(^|\n|\s)CONSOLE($|\n|\s)", full_text, re.I))

    if platform_console and not platform_pc:
        return None

    # ✅ KAKAO 전용 제외
    if "KAKAO" in full_text.upper() and not platform_pc:
        return None

    desc = meta_desc

    if not desc:
        for line in lines[80:220]:
            if line.startswith("Image:"):
                continue

            up = line.upper()

            if up in ("PREV", "NEXT", "GO BACK TO LIST"):
                break

            if len(line) >= 20:
                desc = line
                break

    return {
        "id": article_id_from_url(url),
        "title": trim_text(title, 240),
        "date": clean_text(date),
        "category": category,
        "category_label": category_label(category),
        "url": url,
        "description": trim_text(desc, 360),
        "image_url": image_url,
    }


def collect_article_urls_from_page(url: str, limit: int = 16) -> list[str]:
    try:
        raw = fetch_html(url)
    except Exception as e:
        print(f"[WARN] list fetch failed url={url} err={type(e).__name__}: {e}")
        return []

    soup = BeautifulSoup(raw, "html.parser")
    urls = []

    def add_url(href: str):
        href = abs_pubg_url(href)

        if not href:
            return

        # ✅ 한국/영문 news 모두 허용
        if not re.search(r"/(?:ko|en)/news/\d+", href):
            return

        if href not in urls:
            urls.append(href)

    for a in soup.find_all("a", href=True):
        add_url(a.get("href"))

    # HTML 안에 직접 들어있는 링크도 추가
    for m in re.finditer(r'href=["\']([^"\']*/(?:ko|en)/news/\d+[^"\']*)["\']', raw):
        add_url(m.group(1))

    # Next.js JSON 안의 escaped URL 대응
    for m in re.finditer(r'\\?"url\\?"\s*:\s*\\?"([^"\\]*(?:/ko/news/|/en/news/)\d+[^"\\]*)\\?"', raw):
        add_url(m.group(1).replace("\\/", "/"))

    return urls[:limit]

LIST_DATE_RE = re.compile(
    r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}"
)

LIST_CATEGORY_TO_KIND = {
    "공지": "notice",
    "공지사항": "notice",
    "ANNOUNCEMENT": "notice",
    "패치노트": "patch_notes",
    "PATCH NOTES": "patch_notes",
    "LABS": "labs",
    "개발일지": "dev_notes",
    "DEV LETTER": "dev_notes",
    "이벤트": "event",
    "EVENT": "event",
}

LIST_NOISE_WORDS = {
    "뉴스",
    "이벤트",
    "전체",
    "공지",
    "공지사항",
    "패치노트",
    "아케이드",
    "개발일지",
    "유니버스",
    "진행 중",
    "종료",
    "당첨자 발표",
    "PC",
    "콘솔",
    "CONSOLE",
    "ESPORTS",
    "이스포츠",
    "제목+본문",
    "TITLE+CONTENT",
    "더 보기",
    "PLAY NOW",
}


def _is_list_noise_line(s: str) -> bool:
    s = clean_text(s)

    if not s:
        return True

    if s in LIST_NOISE_WORDS:
        return True

    if re.fullmatch(r"D-\d+", s):
        return True

    if s.startswith("Image:"):
        return True

    if s.startswith("이미지:"):
        return True

    return False


def _kind_matches_list_category(request_kind: str, found_kind: str) -> bool:
    request_kind = str(request_kind or "").strip()
    found_kind = str(found_kind or "").strip()

    if request_kind == found_kind:
        return True

    return False


def _stable_list_article_id(kind: str, title: str, date: str) -> str:
    raw = f"{kind}:{date}:{title}"
    article_id = re.sub(r"\W+", "_", raw).strip("_")

    if not article_id:
        article_id = f"{kind}_{date}"

    return article_id[:180]


def collect_articles_from_list_page(page_url: str, kind: str, limit: int = 3) -> list[dict]:
    """
    PUBG 목록 페이지 fallback 파서.

    PUBG 공식 목록 페이지에서 상세글 href가 안 잡히는 경우,
    페이지에 노출된 제목/날짜/카테고리 텍스트만으로 알림용 article dict를 만든다.

    이 fallback은 상세글 URL 대신 목록 페이지 URL을 사용한다.
    """
    try:
        raw = fetch_html(page_url)
    except Exception as e:
        print(f"[WARN] list text fetch failed url={page_url} err={type(e).__name__}: {e}")
        return []

    lines = soup_text_lines(raw)

    if not lines:
        return []

    # ✅ 목록 시작점 이후만 본다.
    start_idx = 0
    for i, line in enumerate(lines):
        if clean_text(line) in ("제목+본문", "TITLE+CONTENT"):
            start_idx = i + 1
            break

    scan_lines = lines[start_idx:]
    articles = []
    seen = set()

    for i, line in enumerate(scan_lines):
        line = clean_text(line)
        m = LIST_DATE_RE.search(line)

        if not m:
            continue

        date = m.group(0).replace("-", ".").replace("/", ".")

        # ✅ 날짜 바로 위쪽에서 카테고리 라인 찾기
        cat_idx = -1
        found_label = ""
        found_kind = ""

        for j in range(i - 1, max(-1, i - 8), -1):
            t = clean_text(scan_lines[j])
            mapped = LIST_CATEGORY_TO_KIND.get(t)

            if mapped:
                cat_idx = j
                found_label = t
                found_kind = mapped
                break

        if cat_idx < 0:
            continue

        if not _kind_matches_list_category(kind, found_kind):
            continue

        # ✅ 카테고리 위쪽에서 제목/설명 후보 수집
        candidates = []

        for j in range(cat_idx - 1, max(-1, cat_idx - 10), -1):
            t = clean_text(scan_lines[j])

            if not t:
                continue

            # 이전 글 영역으로 넘어가면 중단
            if LIST_DATE_RE.search(t):
                break

            if t in LIST_CATEGORY_TO_KIND:
                break

            if t in ("제목+본문", "TITLE+CONTENT", "더 보기"):
                break

            if _is_list_noise_line(t):
                continue

            candidates.append(t)

            if len(candidates) >= 3:
                break

        candidates.reverse()

        if not candidates:
            continue

        title = candidates[0]
        desc = candidates[1] if len(candidates) >= 2 else ""

        # ✅ e스포츠 글은 공지성 목록에서 제외
        target_text = f"{title} {desc} {found_label}".lower()
        if kind in ("notice", "patch_notes", "labs", "dev_notes"):
            if "esports" in target_text or "e스포츠" in target_text or "이스포츠" in target_text:
                continue

        article_id = _stable_list_article_id(kind, title, date)

        if article_id in seen:
            continue

        seen.add(article_id)

        articles.append({
            "id": article_id,
            "title": trim_text(title, 240),
            "date": clean_text(date),
            "category": kind,
            "category_label": category_label(kind),
            "url": page_url,
            "description": trim_text(desc or "자세한 내용은 PUBG 공식 홈페이지에서 확인하세요.", 360),
            "image_url": "",
        })

        if len(articles) >= limit:
            break

    print(
        f"[INFO] list fallback parsed kind={kind} "
        f"count={len(articles)} page={page_url}"
    )

    return articles[:limit]


def get_latest_articles(kind: str, limit: int = 3) -> list[dict]:
    """
    kind:
      - notice
      - event
      - patch_notes
      - labs
      - dev_notes

    처리 순서:
    1) 기존 방식: 목록 페이지에서 /ko/news/숫자 상세 URL 추출
    2) 실패 시 fallback: 목록 페이지 텍스트에서 제목/날짜/카테고리 직접 추출
    """
    info = PUBG_KO_CATEGORY_URLS.get(kind) or PUBG_KO_CATEGORY_URLS["notice"]
    pages = list(info.get("urls") or [])

    articles = []
    seen_ids = set()

    # ✅ 1차: 기존 상세 URL 파싱 방식
    for page in pages:
        urls = collect_article_urls_from_page(page, limit=24)
        print(f"[INFO] detail url scan kind={kind} page={page} url_count={len(urls)}")

        for url in urls:
            article = parse_article(url, forced_category=kind)

            if not article:
                continue

            article_id = str(article.get("id") or "").strip()

            if not article_id:
                continue

            if article_id in seen_ids:
                continue

            title_l = article.get("title", "").lower()
            desc_l = article.get("description", "").lower()
            cat_l = article.get("category_label", "").lower()

            # ✅ 이벤트는 이벤트성 글만 최대한 필터링
            if kind == "event":
                target = f"{title_l} {desc_l} {cat_l}"
                event_keywords = (
                    "이벤트",
                    "보상",
                    "미션",
                    "패스",
                    "event",
                    "reward",
                    "mission",
                    "pass",
                    "challenge",
                )

                if not any(k.lower() in target for k in event_keywords):
                    continue

            # ✅ e스포츠는 공지에서 제외
            if kind in ("notice", "patch_notes", "labs", "dev_notes"):
                if "esports" in title_l or "e스포츠" in title_l or "이스포츠" in title_l:
                    continue

            seen_ids.add(article_id)
            articles.append(article)

            if len(articles) >= limit:
                return articles[:limit]

    # ✅ 2차: 상세 URL이 안 잡히는 경우 목록 텍스트 fallback
    if not articles:
        print(f"[INFO] detail url mode empty. fallback to list text parser kind={kind}")

        for page in pages:
            fallback_articles = collect_articles_from_list_page(page, kind, limit=limit)

            for article in fallback_articles:
                article_id = str(article.get("id") or "").strip()

                if not article_id:
                    continue

                if article_id in seen_ids:
                    continue

                seen_ids.add(article_id)
                articles.append(article)

                if len(articles) >= limit:
                    return articles[:limit]

    return articles[:limit]


def find_latest_map_report_url() -> str:
    candidates = []

    # ✅ 한국 페이지 우선
    pages = [
        "https://www.pubg.com/ko/news?category=notice",
        "https://www.pubg.com/ko/news?category=patch_notes",
        "https://www.pubg.com/ko/news",
        "https://www.pubg.com/en/news",
    ]

    for page in pages:
        for url in collect_article_urls_from_page(page, limit=30):
            try:
                article = parse_article(url)
            except Exception:
                article = None

            if not article:
                continue

            title = article.get("title", "")
            title_l = title.lower()

            if "map service report" in title_l or "맵 서비스 리포트" in title:
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
    block = lines_between(
        lines,
        r"^(Schedule|일정)$",
        (
            r"^(Normal Match|일반전|일반 매치)$",
            r"^일반 매치$",
        )
    )

    result = []

    date_patterns = (
        # 2026.06.17 / 2026-06-17 / 2026/6/17
        r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}",
        # 26/6/17 / 26.6.17 / 26-6-17
        r"\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}",
        # June 17
        r"[A-Za-z]+\s+\d{1,2}",
        # 6월 17일
        r"\d{1,2}\s*월\s*\d{1,2}\s*일",
    )

    for i, line in enumerate(block):
        m = re.fullmatch(r"(?:Week|주차)\s*(\d+)|(\d+)\s*주차", line, re.I)
        if not m:
            continue

        week = int(m.group(1) or m.group(2))
        pc_date = ""

        # 현재 공식 페이지 구조:
        # 1주차
        # 26/6/17
        # 26/6/25
        # 2주차
        # 26/6/24
        for j in range(i + 1, min(i + 10, len(block))):
            t = clean_text(block[j])

            if not t:
                continue

            # 다음 주차가 나오면 중단
            if re.fullmatch(r"(?:Week|주차)\s*\d+|\d+\s*주차", t, re.I):
                break

            if any(re.fullmatch(p, t, re.I) for p in date_patterns):
                pc_date = t
                break

        if pc_date:
            result.append({"week": week, "pc_date": pc_date})

    print(f"[INFO] parsed map schedule={result}")
    return result


def parse_normal_as_maps(lines: list[str]) -> dict[int, list[str]]:
    normal_block = lines_between(
        lines,
        r"^(Normal Match|일반전|일반 매치)$",
        (
            r"^(Ranked|경쟁전|랭크)$",
        )
    )

    as_block = lines_between(
        normal_block,
        r"^AS$|^아시아$",
        (
            r"^SEA$",
            r"^KAKAO$",
            r"^NA$",
            r"^SA$",
            r"^EU$",
            r"^RU$",
            r"^Console",
            r"^콘솔",
        )
    )

    result = {}

    for i, line in enumerate(as_block):
        m = re.fullmatch(r"(?:Week|주차)\s*(\d+)|(\d+)\s*주차", line, re.I)
        if not m:
            continue

        week = int(m.group(1) or m.group(2))
        maps = []

        for t in as_block[i + 1:]:
            if re.fullmatch(r"(?:Week|주차)\s*\d+|\d+\s*주차", t, re.I):
                break

            if t in ("Fixed", "Favored", "Etc.", "고정", "선호", "기타"):
                continue

            if t in MAP_KO:
                maps.append(MAP_KO[t])

        if maps:
            result[week] = maps[:5]

    return result


def parse_ranked_maps(lines: list[str]) -> list[str]:
    ranked_block = lines_between(
        lines,
        r"^(Ranked|경쟁전|랭크)$",
        (
            r"We’ll see you",
            r"PUBG: BATTLEGROUNDS Team",
            r"PUBG: 배틀그라운드 팀",
            r"^PREV$",
            r"^NEXT$",
            r"^이전$",
            r"^다음$",
        )
    )

    text = " ".join(ranked_block)

    maps = []

    for name in MAP_KO:
        if re.search(rf"\b{re.escape(name)}\b", text):
            maps.append(MAP_KO[name])

    order = ["에란겔", "미라마", "태이고", "론도", "비켄디", "데스턴", "사녹", "카라킨", "파라모", "헤이븐"]
    maps = [m for m in order if m in maps]

    return maps


def parse_month_day(s: str, year: int) -> datetime:
    s = clean_text(s)

    # 2026.06.17 / 2026-06-17 / 2026/6/17
    m = re.fullmatch(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        y = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        return datetime(y, month, day, tzinfo=KST)

    # 26/6/17 / 26.6.17 / 26-6-17
    m = re.fullmatch(r"(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        y = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))

        # PUBG 공식 맵 서비스 리포트의 26/6/17 형식 대응
        if y < 100:
            y = 2000 + y

        return datetime(y, month, day, tzinfo=KST)

    # June 17
    try:
        dt = datetime.strptime(f"{year} {s}", "%Y %B %d")
        return dt.replace(tzinfo=KST)
    except Exception:
        pass

    # 6월 17일
    m = re.fullmatch(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        return datetime(year, month, day, tzinfo=KST)

    raise ValueError(f"날짜 파싱 실패: {s}")


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


def embed_footer() -> dict:
    return {
        "text": "기준: Steam / PC / AS 서버 · PUBG 공식 홈페이지",
    }


def discord_post(webhook_url: str, content: str = "", embed: dict | None = None):
    payload = {
        "username": "PUBG 알림",
        "avatar_url": PUBG_ALERT_AVATAR_URL,
        "allowed_mentions": {"parse": []},
    }

    if content:
        payload["content"] = content

    if embed:
        payload["embeds"] = [embed]
    else:
        payload["content"] = content or ""

    r = requests.post(
        webhook_url,
        json=payload,
        timeout=20,
    )
    r.raise_for_status()


def should_send(state: dict, key: str) -> bool:
    return not bool(state.get(key))


def mark_sent(state: dict, key: str):
    state[key] = now_kst().isoformat()


def build_map_rotation_embed(data: dict) -> dict:
    normal_text = " / ".join(data["normal"])
    ranked_text = " / ".join(data["ranked"])

    desc = (
        "이번 주 PUBG 맵 로테이션 정보입니다.\n"
        "공식 맵 서비스 리포트 기준으로 확인한 내용을 정리했습니다."
    )

    if data.get("fallback"):
        desc += "\n\n⚠️ 공식 페이지 파싱에 실패하여 저장된 기본 맵 정보로 안내합니다."

    return {
        "color": PUBG_COLOR_MAP,
        "author": {
            "name": "PUBG: BATTLEGROUNDS",
            "url": PUBG_HOME,
        },
        "title": "🗺️ PUBG 맵 로테이션 안내",
        "url": data.get("source_url") or PUBG_MAP_REPORT_FALLBACK_URL,
        "description": desc,
        "fields": [
            {
                "name": "기준",
                "value": "Steam / PC / AS 서버",
                "inline": True,
            },
            {
                "name": "기간",
                "value": f"{format_date(data['start'])} ~ {format_date(data['end'])}",
                "inline": True,
            },
            {
                "name": "이번 주 일반전 맵",
                "value": normal_text or "-",
                "inline": False,
            },
            {
                "name": "이번 주 경쟁전 맵",
                "value": ranked_text or "-",
                "inline": False,
            },
        ],
        "footer": embed_footer(),
        "timestamp": now_kst().isoformat(),
    }


def build_article_embed(article: dict, kind: str) -> dict:
    category = article.get("category") or kind

    primary_label = primary_category_label(category)
    secondary_label = secondary_category_label(category)
    secondary_tag = secondary_category_hashtag(category)

    emoji = category_main_emoji(category)
    color = category_color(category)

    article_title = article.get("title") or "PUBG 공식 공지"
    article_desc = article.get("description") or "자세한 내용은 PUBG 공식 홈페이지에서 확인하세요."

    # ✅ Embed 제목은 상위 분류 기준으로 통일
    # 예: 📢 PUBG 공지사항 안내 #패치노트
    # 예: 🎁 PUBG 이벤트 안내
    if primary_alert_kind(category) == "notice":
        embed_title = f"{emoji} PUBG {primary_label} 안내 {secondary_tag}"
    else:
        embed_title = f"{emoji} PUBG {primary_label} 안내"

    # ✅ 본문 첫 줄에 실제 공식 글 제목 표시
    description = f"**{article_title}**"

    if article_desc:
        description += f"\n\n{article_desc}"

    embed = {
        "color": color,
        "author": {
            "name": "PUBG: BATTLEGROUNDS",
            "url": PUBG_HOME,
        },
        "title": embed_title,
        "url": article.get("url") or PUBG_NEWS_URL,
        "description": description,
        "fields": [
            {
                "name": "알림 분류",
                "value": primary_label,
                "inline": True,
            },
            {
                "name": "세부 타입",
                "value": secondary_tag if primary_alert_kind(category) == "notice" else secondary_label,
                "inline": True,
            },
            {
                "name": "게시일",
                "value": article.get("date") or "-",
                "inline": True,
            },
            {
                "name": "기준",
                "value": "Steam / PC / AS 서버",
                "inline": True,
            },
            {
                "name": "바로가기",
                "value": f"[공식 홈페이지에서 보기]({article.get('url') or PUBG_NEWS_URL})",
                "inline": False,
            },
        ],
        "footer": embed_footer(),
        "timestamp": now_kst().isoformat(),
    }

    image_url = article.get("image_url") or ""

    if image_url:
        embed["image"] = {"url": image_url}

    return embed


def send_map_rotation_for_guild(gid: str, guild_cfg: dict, state: dict):
    data = get_current_map_rotation(guild_cfg)

    # ✅ 맵 로테이션도 알림 시작일 이전 주차면 최초 발송하지 않음
    enabled_date = _parse_date_yyyy_mm_dd(guild_cfg.get("enabled_since_date"))

    if enabled_date:
        try:
            rotation_end_date = data["end"].astimezone(KST).date()

            # 이번 로테이션 기간이 알림 시작일보다 완전히 이전이면 스킵
            if rotation_end_date < enabled_date:
                print(
                    "[SKIP] map before guild enabled date "
                    f"gid={gid} "
                    f"enabled_since_date={guild_cfg.get('enabled_since_date')} "
                    f"rotation={format_date(data['start'])}~{format_date(data['end'])}"
                )
                return
        except Exception as e:
            print(f"[WARN] map enabled date filter failed gid={gid} err={type(e).__name__}: {e}")

    key = f"{gid}:map_rotation:{format_date(data['start'])}:{format_date(data['end'])}"

    if not should_send(state, key):
        print(f"[SKIP] map already sent gid={gid} key={key}")
        return

    embed = build_map_rotation_embed(data)

    discord_post(guild_cfg["webhook_url"], embed=embed)
    mark_sent(state, key)

    print(f"[SENT] map rotation gid={gid} week={data['week']} fallback={data['fallback']}")


def article_state_keys(gid: str, kind: str, article_id: str) -> list[str]:
    """
    ✅ 중복 발송 방지 키
    - notice / patch_notes / labs / dev_notes 는 상위 분류 notice로 묶어서 중복 방지
    - 기존에 patch_notes/labs/dev_notes 키로 저장된 기록도 같이 확인해서 업데이트 직후 재발송 방지
    """
    primary = primary_alert_kind(kind)

    keys = [
        f"{gid}:{primary}:{article_id}",
    ]

    legacy_key = f"{gid}:{kind}:{article_id}"

    if legacy_key not in keys:
        keys.append(legacy_key)

    return keys


def article_already_sent(state: dict, gid: str, kind: str, article_id: str) -> bool:
    return any(bool(state.get(k)) for k in article_state_keys(gid, kind, article_id))


def mark_article_sent(state: dict, gid: str, kind: str, article_id: str):
    sent_at = now_kst().isoformat()

    for key in article_state_keys(gid, kind, article_id):
        state[key] = sent_at


def send_articles_for_guild(gid: str, guild_cfg: dict, kind: str, state: dict):
    articles = get_latest_articles(kind, limit=3)

    label = category_label(kind)

    if not articles:
        print(f"[INFO] no articles found gid={gid} kind={kind} label={label}")
        return

    for article in articles:
        article_id = str(article.get("id") or "").strip()

        if not article_id:
            continue

        # ✅ 알림을 켠 날짜/시간 이전 글은 발송하지 않음
        # - 신규 서버 최초 설정 시 과거 글이 한꺼번에 발송되는 문제 방지
        # - OFF 후 다시 ON 하면 다시 켠 날짜/시간 기준으로 필터링됨
        if not _is_after_guild_enabled_time(article, guild_cfg):
            print(
                "[SKIP] before guild enabled time "
                f"gid={gid} "
                f"kind={kind} "
                f"enabled_since={guild_cfg.get('enabled_since')} "
                f"enabled_since_date={guild_cfg.get('enabled_since_date')} "
                f"article_date={article.get('date')} "
                f"id={article_id} "
                f"title={article.get('title')}"
            )
            continue

        # ✅ 공지사항 그룹(notice/patch_notes/labs/dev_notes)은 하나로 묶어서 중복 방지
        if article_already_sent(state, gid, kind, article_id):
            print(f"[SKIP] {kind} already sent gid={gid} id={article_id}")
            continue

        embed = build_article_embed(article, kind)

        discord_post(guild_cfg["webhook_url"], embed=embed)
        mark_article_sent(state, gid, kind, article_id)

        primary = primary_category_label(kind)
        secondary = secondary_category_hashtag(kind)

        print(
            f"[SENT] {primary} {secondary} gid={gid} "
            f"id={article_id} title={article.get('title')}"
        )


def enabled_types_from_config(guild_cfg: dict) -> dict:
    types = guild_cfg.get("types") or {}

    result = {
        "map_rotation": bool(types.get("map_rotation")),
        "notice": bool(types.get("notice")),
        "event": bool(types.get("event")),
    }

    # ✅ 새 카테고리 세분화 설정이 Secret에 들어온 경우 그대로 사용
    # 아직 봇 설정 UI가 notice/event만 저장하더라도 아래 기본값으로 작동함.
    result["patch_notes"] = bool(types.get("patch_notes", result["notice"]))
    result["labs"] = bool(types.get("labs", result["notice"]))
    result["dev_notes"] = bool(types.get("dev_notes", result["notice"]))

    return result

# =========================
# 🧪 PUBG 알림 테스트 모드
# - 실제 웹훅으로 발송
# - pubg_sent.json 중복 발송 기록은 건드리지 않음
# - 각 카테고리 최신글 1개씩 테스트 발송
# =========================
def env_bool(name: str, default: bool = False) -> bool:
    v = str(os.getenv(name, "")).strip().lower()

    if not v:
        return default

    return v in ("1", "true", "yes", "y", "on", "enable", "enabled", "테스트", "켜기")


def env_csv(name: str, default: list[str]) -> list[str]:
    raw = str(os.getenv(name, "")).strip()

    if not raw:
        return list(default)

    return [
        x.strip()
        for x in raw.split(",")
        if x.strip()
    ]


def _mask_webhook_for_log(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return "NO"

    if not url.startswith("http"):
        return "INVALID"

    return "YES"


def _normalize_guild_cfg_for_alert(cfg: dict) -> dict:
    """
    PUBG_ALERT_CONFIG_JSON 안의 guild 설정을 알림 스크립트가 쓰는 형태로 정규화한다.

    지원 구조:
    1) 정상 Actions용 구조:
       {
         "enabled": true,
         "webhook_url": "...",
         "enabled_since": "2026-07-08T09:00:00Z",
         "enabled_since_date": "2026-07-08",
         "types": {...}
       }

    2) 로컬 guild_data 구조가 실수로 들어간 경우:
       {
         "pubg_alert": {
           "enabled": true,
           "webhook_url": "...",
           "enabled_since": "2026-07-08T09:00:00Z",
           "enabled_since_date": "2026-07-08",
           "types": {...}
         }
       }
    """
    if not isinstance(cfg, dict):
        return {}

    pa = cfg.get("pubg_alert")
    if not isinstance(pa, dict):
        pa = {}

    webhook_url = str(cfg.get("webhook_url") or pa.get("webhook_url") or "").strip()

    types = cfg.get("types")
    if not isinstance(types, dict):
        types = pa.get("types")
    if not isinstance(types, dict):
        types = {}

    enabled = cfg.get("enabled")
    if enabled is None:
        enabled = pa.get("enabled")

    channel_id = cfg.get("channel_id")
    if channel_id is None:
        channel_id = pa.get("channel_id")

    channel_name = cfg.get("channel_name")
    if not channel_name:
        channel_name = pa.get("channel_name")

    enabled_since = str(
        cfg.get("enabled_since")
        or pa.get("enabled_since")
        or ""
    ).strip()

    enabled_since_date = str(
        cfg.get("enabled_since_date")
        or pa.get("enabled_since_date")
        or ""
    ).strip()

    normalized = dict(cfg)

    normalized["enabled"] = bool(enabled)
    normalized["webhook_url"] = webhook_url
    normalized["channel_id"] = channel_id
    normalized["channel_name"] = str(channel_name or "")

    # ✅ 알림 시작 기준
    # - 신규 서버가 과거 공지를 최초 1회 발송하지 않도록 사용
    # - OFF 후 다시 ON 하면 봇 코드에서 새 값으로 갱신되어 들어온다.
    normalized["enabled_since"] = enabled_since
    normalized["enabled_since_date"] = enabled_since_date

    normalized["types"] = {
        "map_rotation": bool(types.get("map_rotation")),
        "notice": bool(types.get("notice")),
        "event": bool(types.get("event")),
        "patch_notes": bool(types.get("patch_notes", types.get("notice"))),
        "labs": bool(types.get("labs", types.get("notice"))),
        "dev_notes": bool(types.get("dev_notes", types.get("notice"))),
    }

    normalized["platform"] = normalized.get("platform") or pa.get("platform") or "steam"
    normalized["device"] = normalized.get("device") or pa.get("device") or "pc"
    normalized["server_region"] = normalized.get("server_region") or pa.get("server_region") or "as"

    normalized["normal_maps"] = (
        normalized.get("normal_maps")
        or pa.get("normal_maps")
        or ["에란겔", "태이고", "미라마", "사녹", "비켄디"]
    )
    normalized["ranked_maps"] = (
        normalized.get("ranked_maps")
        or pa.get("ranked_maps")
        or ["에란겔", "미라마", "태이고", "론도"]
    )
    normalized["map_report_url"] = (
        normalized.get("map_report_url")
        or pa.get("map_report_url")
        or PUBG_MAP_REPORT_FALLBACK_URL
    )

    return normalized


def _debug_test_config_summary(config: dict):
    guilds = config.get("guilds") or {}

    print(f"[TEST][CONFIG] guild_count={len(guilds)}")

    for gid, raw_cfg in guilds.items():
        if not isinstance(raw_cfg, dict):
            print(f"[TEST][CONFIG] gid={gid} cfg=NOT_DICT")
            continue

        pa = raw_cfg.get("pubg_alert")
        if not isinstance(pa, dict):
            pa = {}

        top_webhook = str(raw_cfg.get("webhook_url") or "").strip()
        nested_webhook = str(pa.get("webhook_url") or "").strip()

        normalized = _normalize_guild_cfg_for_alert(raw_cfg)

        print(
            f"[TEST][CONFIG] gid={gid} "
            f"top_enabled={raw_cfg.get('enabled')} "
            f"nested_enabled={pa.get('enabled')} "
            f"top_webhook={_mask_webhook_for_log(top_webhook)} "
            f"nested_webhook={_mask_webhook_for_log(nested_webhook)} "
            f"normalized_enabled={normalized.get('enabled')} "
            f"normalized_webhook={_mask_webhook_for_log(normalized.get('webhook_url'))} "
            f"types={normalized.get('types')}"
        )


def get_test_guild_cfg(config: dict) -> tuple[str, dict]:
    """
    테스트에 사용할 guild 설정 선택.

    우선순위:
    1) PUBG_ALERT_TEST_WEBHOOK_URL 직접 지정
    2) PUBG_ALERT_TEST_GUILD_ID에 해당하는 guild
    3) enabled=True 이고 webhook_url 있는 첫 guild
    4) webhook_url 있는 첫 guild

    추가 대응:
    - Secret에 실수로 로컬 guild_data 구조(pubg_alert.webhook_url)가 들어간 경우도 지원
    """
    _debug_test_config_summary(config)

    direct_webhook = str(os.getenv("PUBG_ALERT_TEST_WEBHOOK_URL", "")).strip()

    if direct_webhook:
        return "TEST_WEBHOOK", {
            "enabled": True,
            "webhook_url": direct_webhook,
            "platform": "steam",
            "device": "pc",
            "server_region": "as",
            "types": {
                "map_rotation": True,
                "notice": True,
                "event": True,
                "patch_notes": True,
                "labs": True,
                "dev_notes": True,
            },
            "normal_maps": ["에란겔", "태이고", "미라마", "사녹", "비켄디"],
            "ranked_maps": ["에란겔", "태이고", "미라마", "론도"],
        }

    guilds = config.get("guilds") or {}
    test_gid = str(os.getenv("PUBG_ALERT_TEST_GUILD_ID", "")).strip()

    if test_gid:
        raw_cfg = guilds.get(test_gid)
        cfg = _normalize_guild_cfg_for_alert(raw_cfg)

        if isinstance(cfg, dict) and str(cfg.get("webhook_url") or "").strip():
            return test_gid, cfg

        raise RuntimeError(
            f"PUBG_ALERT_TEST_GUILD_ID={test_gid} 서버 설정을 찾지 못했거나 webhook_url이 없습니다. "
            f"Actions 로그의 [TEST][CONFIG] 출력을 확인하세요."
        )

    for gid, raw_cfg in guilds.items():
        cfg = _normalize_guild_cfg_for_alert(raw_cfg)

        if cfg.get("enabled") and str(cfg.get("webhook_url") or "").strip():
            return str(gid), cfg

    for gid, raw_cfg in guilds.items():
        cfg = _normalize_guild_cfg_for_alert(raw_cfg)

        if str(cfg.get("webhook_url") or "").strip():
            return str(gid), cfg

    raise RuntimeError(
        "테스트에 사용할 webhook_url을 찾지 못했습니다. "
        "PUBG_ALERT_CONFIG_JSON 안에 guild 설정은 있지만, "
        "webhook_url 또는 pubg_alert.webhook_url이 없습니다. "
        "Actions 로그의 [TEST][CONFIG] 출력을 확인하세요."
    )


def build_test_empty_embed(kind: str, reason: str) -> dict:
    label = category_label(kind)
    emoji = category_emoji(kind)

    return {
        "color": PUBG_COLOR_NOTICE,
        "author": {
            "name": "PUBG: BATTLEGROUNDS",
            "url": PUBG_HOME,
        },
        "title": f"🧪 PUBG 알림 테스트 · {emoji} {label}",
        "description": (
            f"카테고리 `{kind}` 테스트 중 최신글을 찾지 못했습니다.\n\n"
            f"사유: {reason}"
        ),
        "fields": [
            {
                "name": "테스트 결과",
                "value": "최신글 없음 / 파싱 실패 / 필터링 제외",
                "inline": False,
            },
            {
                "name": "중복 기록",
                "value": "테스트 모드이므로 pubg_sent.json에 저장하지 않음",
                "inline": False,
            },
        ],
        "footer": embed_footer(),
        "timestamp": now_kst().isoformat(),
    }


def send_test_article_for_kind(webhook_url: str, kind: str):
    """
    특정 카테고리 최신글 1개를 테스트 발송.
    실제 알림과 같은 build_article_embed()를 사용한다.
    """
    articles = get_latest_articles(kind, limit=1)

    if not articles:
        embed = build_test_empty_embed(kind, "get_latest_articles() 결과가 비어있습니다.")
        discord_post(
            webhook_url,
            content=f"🧪 PUBG 알림 테스트 · `{kind}`",
            embed=embed,
        )
        print(f"[TEST][EMPTY] kind={kind}")
        return

    article = articles[0]

    # ✅ forced_category가 누락된 경우 대비
    article["category"] = article.get("category") or kind
    article["category_label"] = article.get("category_label") or category_label(kind)

    embed = build_article_embed(article, kind)

    # ✅ 테스트임을 명확히 표시
    embed.setdefault("fields", [])
    embed["fields"].insert(
        0,
        {
            "name": "🧪 테스트 발송",
            "value": (
                "각 카테고리 최신글 1개 테스트입니다.\n"
                "`pubg_sent.json` 중복 발송 기록에는 저장하지 않습니다."
            ),
            "inline": False,
        }
    )

    discord_post(
        webhook_url,
        content=f"🧪 PUBG 알림 테스트 · `{kind}`",
        embed=embed,
    )

    print(
        f"[TEST][SENT] kind={kind} "
        f"id={article.get('id')} title={article.get('title')}"
    )


def send_test_map_rotation(webhook_url: str, guild_cfg: dict):
    data = get_current_map_rotation(guild_cfg)
    embed = build_map_rotation_embed(data)

    embed.setdefault("fields", [])
    embed["fields"].insert(
        0,
        {
            "name": "🧪 테스트 발송",
            "value": (
                "맵 로테이션 테스트입니다.\n"
                "`pubg_sent.json` 중복 발송 기록에는 저장하지 않습니다."
            ),
            "inline": False,
        }
    )

    discord_post(
        webhook_url,
        content="🧪 PUBG 알림 테스트 · `map_rotation`",
        embed=embed,
    )

    print(
        f"[TEST][SENT] kind=map_rotation "
        f"week={data.get('week')} fallback={data.get('fallback')}"
    )


def run_test_mode(config: dict):
    gid, guild_cfg = get_test_guild_cfg(config)
    webhook_url = str(guild_cfg.get("webhook_url") or "").strip()

    if not webhook_url:
        raise RuntimeError("테스트용 webhook_url이 비어있습니다.")

    # ✅ 테스트할 카테고리
    # 기본값: 공지사항 묶음 4개 + 이벤트 + 맵로테이션
    test_kinds = env_csv(
        "PUBG_ALERT_TEST_KINDS",
        ["notice", "patch_notes", "labs", "dev_notes", "event", "map_rotation"]
    )

    # ✅ Steam / PC / AS 서버 기준 강제
    guild_cfg["platform"] = "steam"
    guild_cfg["device"] = "pc"
    guild_cfg["server_region"] = "as"

    print(f"[TEST] PUBG alert test mode started gid={gid} kinds={test_kinds}")

    for kind in test_kinds:
        kind = str(kind or "").strip()

        if not kind:
            continue

        try:
            if kind == "map_rotation":
                send_test_map_rotation(webhook_url, guild_cfg)
            else:
                send_test_article_for_kind(webhook_url, kind)

        except Exception as e:
            print(f"[TEST][ERROR] kind={kind} err={type(e).__name__}: {e}")

            try:
                embed = build_test_empty_embed(kind, f"{type(e).__name__}: {e}")
                discord_post(
                    webhook_url,
                    content=f"🧪 PUBG 알림 테스트 실패 · `{kind}`",
                    embed=embed,
                )
            except Exception as send_e:
                print(
                    f"[TEST][ERROR] failed to send error embed "
                    f"kind={kind} err={type(send_e).__name__}: {send_e}"
                )

    print("[TEST] PUBG alert test mode finished")


def main():
    config = load_config()

    # ✅ 테스트 모드
    # - 실제 웹훅으로 발송
    # - state/pubg_sent.json 저장 안 함
    if env_bool("PUBG_ALERT_TEST_MODE", False):
        run_test_mode(config)
        return

    state = load_state()

    guilds = config.get("guilds") or {}

    print(f"[CONFIG] normal mode guild_count={len(guilds)}")

    for gid, raw_guild_cfg in guilds.items():
        if not isinstance(raw_guild_cfg, dict):
            print(f"[SKIP] gid={gid} cfg=NOT_DICT")
            continue

        # ✅ 테스트 모드와 동일하게 Secret 구조 정규화
        guild_cfg = _normalize_guild_cfg_for_alert(raw_guild_cfg)

        webhook_url = str(guild_cfg.get("webhook_url") or "").strip()

        print(
            f"[CONFIG] gid={gid} "
            f"enabled={guild_cfg.get('enabled')} "
            f"webhook={_mask_webhook_for_log(webhook_url)} "
            f"enabled_since={guild_cfg.get('enabled_since')} "
            f"enabled_since_date={guild_cfg.get('enabled_since_date')} "
            f"types={guild_cfg.get('types')}"
        )

        if not guild_cfg.get("enabled"):
            continue

        if not webhook_url:
            continue

        # ✅ Steam / PC / AS 서버 기준 강제
        guild_cfg["platform"] = "steam"
        guild_cfg["device"] = "pc"
        guild_cfg["server_region"] = "as"

        types = enabled_types_from_config(guild_cfg)

        if types.get("map_rotation"):
            send_map_rotation_for_guild(str(gid), guild_cfg, state)

        # ✅ 공지사항: notice
        if types.get("notice"):
            send_articles_for_guild(str(gid), guild_cfg, "notice", state)

        # ✅ 패치노트: patch_notes
        if types.get("patch_notes"):
            send_articles_for_guild(str(gid), guild_cfg, "patch_notes", state)

        # ✅ LABS: labs
        if types.get("labs"):
            send_articles_for_guild(str(gid), guild_cfg, "labs", state)

        # ✅ 개발일지: dev_notes
        if types.get("dev_notes"):
            send_articles_for_guild(str(gid), guild_cfg, "dev_notes", state)

        # ✅ 기존 이벤트 설정 호환
        if types.get("event"):
            send_articles_for_guild(str(gid), guild_cfg, "event", state)

    save_state(state)


if __name__ == "__main__":
    main()
