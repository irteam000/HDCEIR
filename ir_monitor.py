"""
ir_monitor.py — 경쟁사 IR 자료 변경 감지 엔진

competitor_monitor.py에서 import하여 사용한다.
RSS/HTML에서 IR 자료(보도자료·실적·IR·공시 등) 목록을 수집하고,
이전 실행과 비교해 NEW / UPDATED / REMOVED 를 감지한다.

외부 유료 API를 사용하지 않으며 requests + feedparser + beautifulsoup4만 쓴다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class IRUpdate:
    company: str
    title: str
    link: str
    published: Optional[dt.datetime] = None
    source_type: str = "html"          # rss | html | pdf | ppt
    is_new: bool = False
    is_updated: bool = False
    is_removed: bool = False
    detected_at: Optional[dt.datetime] = None
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IR-Monitor/1.0; +https://example.com/bot)"
}
TIMEOUT = 15
MAX_RETRY = 2
MAX_PER_COMPANY = 20          # 회사별 캐시 저장 최대
DISPLAY_PER_COMPANY = 10      # 화면 표시 최대
NEW_RETENTION_DAYS = 7        # NEW 상태 유지 기간
REMOVED_MISS_THRESHOLD = 3    # 연속 N회 미발견 시 REMOVED
HISTORY_RETENTION_DAYS = 30   # 히스토리 보관 기간

DOC_EXTENSIONS = (".pdf", ".ppt", ".pptx")

# Auto Discovery 우선순위 키워드 (ir_sources 비어있을 때만)
DISCOVERY_KEYWORDS = [
    "investor relations", "investors", "newsroom", "press release", "media center",
]

# 중요도 태그 규칙 (소문자 부분일치)
TAG_RULES = {
    "실적": ["earnings", "results", "financial results", "guidance", "outlook", "실적", "잠정"],
    "IR": ["presentation", "investor day", "investor presentation", "ir"],
    "ESG": ["esg", "sustainability", "지속가능"],
    "주주환원": ["dividend", "buyback", "share repurchase", "배당", "자사주"],
    "전략": ["acquisition", "merger", "joint venture", "partnership", "investment",
            "factory", "plant", "capacity", "capex", "backlog", "인수", "합병", "수주"],
}


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def _log(level: str, company: str, url: str = "", http: str = "", reason: str = "") -> None:
    msg = f"[{level}] {company}"
    if url:
        msg += f"\n  URL: {url}"
    if http:
        msg += f"\n  HTTP: {http}"
    if reason:
        msg += f"\n  Reason: {reason}"
    print(msg, file=sys.stderr)


def _normalize_title(title: str) -> str:
    """제목 정규화: 소문자, 공백·기호 정리 (유사도 비교·중복제거용)."""
    t = (title or "").lower().strip()
    t = re.sub(r"\.(pdf|ppt|pptx)$", "", t)
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _detect_tags(title: str) -> list[str]:
    low = (title or "").lower()
    tags = []
    for tag, kws in TAG_RULES.items():
        if any(kw in low for kw in kws):
            tags.append(tag)
    return tags


def _source_type_for(link: str) -> str:
    low = link.lower()
    for ext in DOC_EXTENSIONS:
        if low.endswith(ext) or f"{ext}?" in low:
            return ext.lstrip(".")
    return "html"


def _http_get(url: str, company: str) -> Optional[requests.Response]:
    """재시도 포함 GET. 실패 시 None 반환(전체 중단하지 않음)."""
    for attempt in range(MAX_RETRY + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp
            _log("WARN", company, url, str(resp.status_code), resp.reason)
            if resp.status_code in (403, 404):
                return None  # 재시도 무의미
        except requests.RequestException as e:
            _log("WARN", company, url, "-", str(e)[:80])
        time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# 수집: RSS → HTML fallback
# ---------------------------------------------------------------------------

def _parse_rss(url: str, company: str) -> list[IRUpdate]:
    feed = feedparser.parse(url)
    if not feed.entries:
        return []
    out = []
    for e in feed.entries:
        published = None
        if getattr(e, "published_parsed", None):
            published = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
        link = getattr(e, "link", "") or ""
        out.append(IRUpdate(
            company=company,
            title=getattr(e, "title", "").strip(),
            link=link,
            published=published,
            source_type="rss",
        ))
    return out


def _parse_html(url: str, company: str, html: str) -> list[IRUpdate]:
    """HTML에서 자료 링크 추출 (fallback). 사이트 구조가 제각각이라 보수적으로."""
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or len(text) < 8:
            continue
        full = urljoin(url, href)
        if full in seen_links:
            continue
        # 문서 링크(pdf/ppt) 또는 뉴스/IR스러운 링크만
        st = _source_type_for(full)
        looks_relevant = (
            st in ("pdf", "ppt", "pptx")
            or any(k in full.lower() for k in ["news", "press", "release", "ir", "investor", "presentation", "earnings"])
        )
        if not looks_relevant:
            continue
        seen_links.add(full)
        out.append(IRUpdate(
            company=company, title=text, link=full, published=None, source_type=st,
        ))
        if len(out) >= MAX_PER_COMPANY * 2:
            break
    return out


def fetch_ir_updates(company: str, ir_sources: list[str]) -> list[IRUpdate]:
    """한 회사의 IR 자료를 수집. RSS 우선, 실패 시 HTML 파싱. 회사 실패해도 빈 리스트 반환."""
    items: list[IRUpdate] = []
    for src in ir_sources:
        try:
            # 1) RSS 시도
            rss_items = _parse_rss(src, company)
            if rss_items:
                items.extend(rss_items)
                continue
            # 2) HTML fallback
            resp = _http_get(src, company)
            if resp is None:
                continue
            ctype = resp.headers.get("Content-Type", "")
            if "xml" in ctype and not items:
                items.extend(_parse_rss(src, company))
            else:
                items.extend(_parse_html(src, company, resp.text))
        except Exception as e:  # noqa: BLE001 - 한 소스 실패가 전체를 막지 않음
            _log("WARN", company, src, "-", f"parse error: {str(e)[:80]}")
            continue
    return _dedupe(items)[:MAX_PER_COMPANY]


def _dedupe(items: list[IRUpdate]) -> list[IRUpdate]:
    """제목 유사도 90%+ 또는 동일 링크 중복 제거."""
    out: list[IRUpdate] = []
    for it in items:
        dup = False
        for kept in out:
            if it.link == kept.link or _similar(it.title, kept.title) >= 0.90:
                dup = True
                break
        if not dup:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# SEC EDGAR 수집 (미국 상장사 — 봇 차단 없는 공개 API)
# ---------------------------------------------------------------------------

SEC_HEADERS = {"User-Agent": "IR-Monitor research contact@example.com"}


def fetch_sec_filings(company: str, cik: str, lookback_count: int = MAX_PER_COMPANY) -> list[IRUpdate]:
    """SEC EDGAR submissions API로 미국 상장사 최근 공시를 IRUpdate로 수집.
    cik는 자리수 무관(내부에서 10자리 zero-pad). 공개 데이터라 차단 없음."""
    if not cik:
        return []
    cik10 = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            _log("WARN", company, url, str(resp.status_code), "SEC submissions fetch 실패")
            return []
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        descs = recent.get("primaryDocDescription", [])
        out: list[IRUpdate] = []
        cik_int = str(int(cik10))
        for i in range(min(len(forms), lookback_count)):
            acc_nodash = accs[i].replace("-", "")
            link = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{docs[i]}"
            desc = descs[i] if i < len(descs) else ""
            title = f"[{forms[i]}] {desc or forms[i]}"
            published = None
            try:
                published = dt.datetime.strptime(dates[i], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            except (ValueError, IndexError):
                pass
            out.append(IRUpdate(
                company=company, title=title, link=link,
                published=published, source_type="sec",
            ))
        return out
    except Exception as e:  # noqa: BLE001
        _log("WARN", company, url, "-", f"SEC parse error: {str(e)[:80]}")
        return []


# ---------------------------------------------------------------------------
# Auto Discovery (ir_sources 비어있을 때만, 로그용)
# ---------------------------------------------------------------------------

def discover_ir_sources(homepage: str, company: str) -> list[str]:
    if BeautifulSoup is None:
        return []
    resp = _http_get(homepage, company)
    if resp is None:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a["href"]
        for kw in DISCOVERY_KEYWORDS:
            if kw in text:
                found.append(urljoin(homepage, href))
                break
    found = list(dict.fromkeys(found))
    if found:
        _log("INFO", company, reason=f"auto-discovered IR candidates: {found[:5]}")
    return found


# ---------------------------------------------------------------------------
# 캐시 + 변경 감지
# ---------------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(d: Optional[dt.datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def _from_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def load_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(path: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def detect_changes(company: str, fresh: list[IRUpdate], cache: dict) -> tuple[list[IRUpdate], dict]:
    """
    캐시와 비교해 각 항목의 NEW/UPDATED 상태를 채우고, REMOVED 후보를 판정.
    cache 구조: { company: { url: {title, published, first_seen, last_seen, miss_count, tags} } }
    반환: (상태가 채워진 현재 항목 리스트, 갱신된 cache)
    """
    now = _now()
    comp_cache = cache.get(company, {})
    seen_urls = set()
    result: list[IRUpdate] = []

    for it in fresh:
        it.tags = _detect_tags(it.title)
        it.detected_at = now
        seen_urls.add(it.link)
        prev = comp_cache.get(it.link)

        if prev is None:
            # URL 변경 가능성: 제목 유사 + 게시일 유사한 기존 항목 찾기 → UPDATED
            matched_key = None
            for old_url, rec in comp_cache.items():
                if old_url in seen_urls:
                    continue
                if _similar(it.title, rec.get("title", "")) >= 0.90:
                    matched_key = old_url
                    break
            if matched_key:
                old = comp_cache[matched_key]
                it.is_updated = True
                comp_cache[it.link] = {
                    "title": it.title,
                    "published": _iso(it.published),
                    "first_seen": old.get("first_seen", _iso(now)),
                    "last_seen": _iso(now),
                    "miss_count": 0,
                    "tags": it.tags,
                }
                # 옛 URL 레코드는 제거(이전됨)
                comp_cache.pop(matched_key, None)
            else:
                it.is_new = True
                comp_cache[it.link] = {
                    "title": it.title,
                    "published": _iso(it.published),
                    "first_seen": _iso(now),
                    "last_seen": _iso(now),
                    "miss_count": 0,
                    "tags": it.tags,
                }
        else:
            # 기존 URL 존재: 제목/게시일 변경 시 UPDATED
            changed = (
                _normalize_title(prev.get("title", "")) != _normalize_title(it.title)
                or (prev.get("published") or "") != (_iso(it.published) or "")
            )
            first_seen = _from_iso(prev.get("first_seen")) or now
            if changed:
                it.is_updated = True
            # NEW 상태 유지: 최초 발견 7일 이내
            if (now - first_seen).days < NEW_RETENTION_DAYS and not it.is_updated:
                it.is_new = True
            comp_cache[it.link] = {
                "title": it.title,
                "published": _iso(it.published),
                "first_seen": prev.get("first_seen", _iso(now)),
                "last_seen": _iso(now),
                "miss_count": 0,
                "tags": it.tags,
            }
        result.append(it)

    # REMOVED 판정: 이번에 안 보인 캐시 항목의 miss_count 증가
    removed: list[IRUpdate] = []
    for url, rec in list(comp_cache.items()):
        if url in seen_urls:
            continue
        rec["miss_count"] = rec.get("miss_count", 0) + 1
        if rec["miss_count"] >= REMOVED_MISS_THRESHOLD:
            ir = IRUpdate(
                company=company, title=rec.get("title", ""), link=url,
                published=_from_iso(rec.get("published")), is_removed=True,
                detected_at=now, tags=rec.get("tags", []),
            )
            removed.append(ir)
            comp_cache.pop(url, None)  # REMOVED 확정 후 캐시에서 제거

    cache[company] = comp_cache
    return result + removed, cache


def update_history(path: str, changes: list[IRUpdate]) -> list[dict]:
    """변경 항목을 히스토리에 누적하고 최근 30일만 유지."""
    now = _now()
    hist = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            hist = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        hist = []

    for c in changes:
        if c.is_new or c.is_updated or c.is_removed:
            state = "NEW" if c.is_new else ("UPDATED" if c.is_updated else "REMOVED")
            hist.append({
                "company": c.company,
                "title": c.title,
                "link": c.link,
                "state": state,
                "detected_at": _iso(c.detected_at or now),
                "tags": c.tags,
            })

    cutoff = now - dt.timedelta(days=HISTORY_RETENTION_DAYS)
    hist = [h for h in hist if (_from_iso(h.get("detected_at")) or now) >= cutoff]

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
    return hist
