"""
경쟁사 모니터링 자동화 MVP (코스피 트랙 + 건설기계 트랙)

[주가 수집 개선판]
- 한국 종목(.KS/.KQ)·코스피 지수: 네이버 금융 API 1순위 → 실패 시 yfinance 폴백
- 해외 종목(CAT/6301.T/600031.SS 등)·환율: yfinance + 재시도/백오프/User-Agent
- 가격을 못 받아도 카드를 숨기지 않고 "데이터 없음"으로 표시
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import feedparser
import requests
import yaml

import ir_monitor as irm

try:
    import yfinance as yf
except ImportError:
    yf = None


@dataclass
class Competitor:
    name: str
    keywords: list[str]
    ticker: str = ""
    region: str = "KR"
    lang: str = ""
    country: str = ""
    ir_sources: list[str] = field(default_factory=list)  # IR 자료 목록 페이지 URL들
    cik: str = ""                                          # 미국 상장사 SEC CIK 번호


@dataclass
class Config:
    company_name: str
    competitors: list[Competitor]
    news_lookback_hours: int = 24
    max_news_per_competitor: int = 5
    news_language: str = "ko"
    news_country: str = "KR"
    report_title: str = "경쟁사 동향 데일리 브리핑"
    # 코스피 트랙 설정
    kospi_ticker: str = "^KS11"
    kospi_keywords: list[str] = field(default_factory=lambda: ["코스피", "KOSPI"])
    # 그룹주 트랙 (HD현대 그룹)
    group_stocks: list[Competitor] = field(default_factory=list)
    # 공시(DART) 설정
    disclosure_lookback_days: int = 30                 # 공시 조회 기간 (뉴스와 별도, 길게)
    disclosure_types: list[str] = field(default_factory=lambda: ["A", "B", "I"])  # 정기공시·주요사항·거래소공시만
    disclosure_max_per_company: int = 15
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    mail_from: str = field(default_factory=lambda: os.getenv("MAIL_FROM", ""))
    mail_to: list[str] = field(default_factory=list)


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    competitors = [
        Competitor(
            name=c["name"],
            keywords=c.get("keywords", [c["name"]]),
            ticker=c.get("ticker", ""),
            region=c.get("region", "KR"),
            lang=c.get("lang", ""),
            country=c.get("country", ""),
            ir_sources=c.get("ir_sources", []),
            cik=str(c.get("cik", "")),
        )
        for c in raw["competitors"]
    ]
    kospi = raw.get("kospi", {})
    group_stocks = [
        Competitor(
            name=c["name"],
            keywords=c.get("keywords", [c["name"]]),
            ticker=c.get("ticker", ""),
            region="KR",
            lang="ko",
            country="KR",
        )
        for c in raw.get("group_stocks", [])
    ]
    return Config(
        company_name=raw["company_name"],
        competitors=competitors,
        news_lookback_hours=raw.get("news_lookback_hours", 24),
        max_news_per_competitor=raw.get("max_news_per_competitor", 5),
        news_language=raw.get("news_language", "ko"),
        news_country=raw.get("news_country", "KR"),
        report_title=raw.get("report_title", "경쟁사 동향 데일리 브리핑"),
        kospi_ticker=kospi.get("ticker", "^KS11"),
        kospi_keywords=kospi.get("keywords", ["코스피", "KOSPI"]),
        group_stocks=group_stocks,
        disclosure_lookback_days=raw.get("disclosure_lookback_days", 30),
        disclosure_types=raw.get("disclosure_types", ["A", "B", "I"]),
        disclosure_max_per_company=raw.get("disclosure_max_per_company", 15),
        mail_to=raw.get("mail_to", []),
    )


@dataclass
class NewsItem:
    title: str
    link: str
    published: Optional[dt.datetime]
    source: str


@dataclass
class StockSnapshot:
    ticker: str
    price: Optional[float] = None
    change_pct: Optional[float] = None
    currency: str = ""
    market_cap: Optional[float] = None
    history: list[float] = field(default_factory=list)   # 최근 1개월 종가 (미니 차트용)
    ytd_pct: Optional[float] = None                       # 연초 대비 수익률 (비교 그래프용)
    per: Optional[float] = None                           # 주가수익비율
    pbr: Optional[float] = None                           # 주가순자산비율
    div_yield: Optional[float] = None                     # 배당수익률(%)
    error: str = ""


def _fetch_news_by_keywords(keywords: list[str], lang: str, country: str,
                            lookback_hours: int, max_items: int) -> list[NewsItem]:
    query = " OR ".join(f'"{k}"' for k in keywords)
    params = {"q": query, "hl": lang, "gl": country, "ceid": f"{country}:{lang}"}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    feed = feedparser.parse(url)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
    items: list[NewsItem] = []
    for entry in feed.entries:
        published = None
        if getattr(entry, "published_parsed", None):
            published = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
            if published < cutoff:
                continue
        source = ""
        if getattr(entry, "source", None):
            source = entry.source.get("title", "")
        items.append(NewsItem(title=entry.title, link=entry.link, published=published, source=source))
        if len(items) >= max_items:
            break
    return items


def fetch_news(comp: Competitor, cfg: Config) -> list[NewsItem]:
    lang = comp.lang or cfg.news_language
    country = comp.country or cfg.news_country
    return _fetch_news_by_keywords(comp.keywords, lang, country,
                                   cfg.news_lookback_hours, cfg.max_news_per_competitor)


def fetch_kospi_news(cfg: Config) -> list[NewsItem]:
    # 코스피 뉴스: 한국어(ko/KR)와 영어(en/US) 양쪽에서 모아 합친다.
    # "코스피"는 한국어 매체, "KOSPI"는 영문 매체에서 더 잘 잡히기 때문.
    per = max(cfg.max_news_per_competitor, 6)
    ko = _fetch_news_by_keywords(cfg.kospi_keywords, "ko", "KR", cfg.news_lookback_hours, per)
    en = _fetch_news_by_keywords(["KOSPI"], "en", "US", cfg.news_lookback_hours, per)
    # 링크 기준 중복 제거 후 합치기
    seen = set()
    merged: list[NewsItem] = []
    for n in ko + en:
        if n.link in seen:
            continue
        seen.add(n.link)
        merged.append(n)
    return merged


# ===========================================================================
# 주가 수집 (개선판) — 네이버 금융 + yfinance 하이브리드
# ===========================================================================

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://m.stock.naver.com/",
    })
    return s


_SESSION = _make_session()


def _retry(fn, tries: int = 3, base_delay: float = 1.5):
    """fn()을 tries번까지 시도. 예외 또는 None이면 백오프 후 재시도."""
    last_exc = None
    for i in range(tries):
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as e:  # noqa
            last_exc = e
        if i < tries - 1:
            time.sleep(base_delay * (2 ** i))
    if isinstance(last_exc, Exception):
        raise last_exc
    return None


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    # 숫자/부호/소수점만 남기고 제거 (콤마, '배', '원', '%', '주', 공백 등)
    import re
    m = re.search(r"[-+]?[\d,]*\.?\d+", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _ticker_to_naver_code(ticker: str) -> Optional[str]:
    """'241560.KS' -> '241560'. 코스피지수 '^KS11' -> 'KOSPI'. 해외는 None."""
    if not ticker:
        return None
    if ticker.upper() in ("^KS11", "KS11"):
        return "KOSPI"
    if ticker.endswith((".KS", ".KQ")):
        return ticker.split(".")[0]
    return None


def _naver_daily_prices(code: str, count: int = 400) -> Optional[list[tuple[str, float]]]:
    """네이버 종목 일봉 → [(YYYYMMDD, 종가)] 오래된→최신. 실패 시 None.
    실제 엔드포인트: api.finance.naver.com/siseJson.naver (텍스트 배열 응답).
    응답 형식: [['날짜','시가','고가','저가','종가','거래량','외국인소진율'], ['20240102', 75000, ...], ...]"""
    end = dt.datetime.now()
    start = end - dt.timedelta(days=int(count * 1.6) + 10)  # 거래일 여유분

    def _call():
        url = ("https://api.finance.naver.com/siseJson.naver"
               f"?symbol={code}&requestType=1"
               f"&startTime={start:%Y%m%d}&endTime={end:%Y%m%d}&timeframe=day")
        resp = _SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        return resp.text

    text = _retry(_call)
    if not text:
        return None
    return _parse_sise_json(text)


def _parse_sise_json(text: str) -> Optional[list[tuple[str, float]]]:
    """siseJson 텍스트 배열을 (YYYYMMDD, 종가) 리스트로. 안전 파싱(ast)."""
    import ast
    try:
        # 응답은 작은따옴표 + 줄바꿈 포함된 파이썬 리스트 리터럴 형태
        cleaned = text.strip()
        rows = ast.literal_eval(cleaned)
    except (ValueError, SyntaxError):
        return None
    if not rows or len(rows) < 2:
        return None
    header = rows[0]
    # 날짜/종가 컬럼 인덱스 찾기 (보통 0=날짜, 4=종가)
    try:
        date_i = header.index("날짜")
    except ValueError:
        date_i = 0
    try:
        close_i = header.index("종가")
    except ValueError:
        close_i = 4
    out: list[tuple[str, float]] = []
    for r in rows[1:]:
        if not isinstance(r, (list, tuple)) or len(r) <= max(date_i, close_i):
            continue
        d = str(r[date_i]).strip().replace("-", "")[:8]
        c = _to_float(r[close_i])
        if d and c is not None:
            out.append((d, c))
    if not out:
        return None
    out.sort(key=lambda x: x[0])
    return out


def _naver_index_prices(index_code: str = "KOSPI", count: int = 400) -> Optional[list[tuple[str, float]]]:
    """네이버 지수 일봉 → [(YYYYMMDD, 종가)]. 실제 엔드포인트: m.stock.naver.com index price.
    페이지네이션으로 count개까지 수집 (페이지당 ~100)."""
    out: list[tuple[str, float]] = []
    page = 1
    page_size = 100
    while len(out) < count and page <= 6:
        def _call(p=page):
            url = (f"https://m.stock.naver.com/api/index/{index_code}/price"
                   f"?pageSize={page_size}&page={p}")
            resp = _SESSION.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            return resp.json()

        data = _retry(_call)
        if not data or not isinstance(data, list):
            break
        added = 0
        for r in data:
            d = r.get("localTradedAt") or r.get("localDate")
            c = _to_float(r.get("closePrice"))
            if d and c is not None:
                out.append((str(d).replace("-", "")[:8], c))
                added += 1
        if added == 0:
            break
        page += 1
    if not out:
        return None
    # 중복 제거 + 정렬
    uniq = dict(out)  # 날짜→종가 (뒤가 덮어씀, 동일값이라 무방)
    series = sorted(uniq.items(), key=lambda x: x[0])
    return series


_INTEGRATION_CACHE: dict[str, Optional[dict]] = {}


def _naver_integration(code: str) -> Optional[dict]:
    """네이버 종목 통합정보 JSON (1종목 1회 캐시)."""
    if code in _INTEGRATION_CACHE:
        return _INTEGRATION_CACHE[code]

    def _call():
        url = f"https://m.stock.naver.com/api/stock/{code}/integration"
        resp = _SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        return resp.json()

    try:
        data = _retry(_call)
    except Exception:
        data = None
    _INTEGRATION_CACHE[code] = data
    return data


def _total_info_value(data: dict, *codes: str) -> Optional[float]:
    """totalInfos에서 주어진 code 키들 중 하나의 값을 float로."""
    if not data:
        return None
    for row in data.get("totalInfos", []):
        if row.get("code") in codes:
            v = _to_float(row.get("value"))
            if v is not None:
                return v
    return None


def _parse_kr_amount(s: str) -> Optional[float]:
    """'7조 206억' / '64,632,619'(백만 아님, 원) 같은 네이버 시총 문자열 → 원 단위 float."""
    if not s:
        return None
    s = str(s).strip()
    # '조'/'억' 한글 표기
    if "조" in s or "억" in s:
        total = 0.0
        import re
        m_jo = re.search(r"([\d,\.]+)\s*조", s)
        m_eok = re.search(r"([\d,\.]+)\s*억", s)
        if m_jo:
            total += float(m_jo.group(1).replace(",", "")) * 1_0000_0000_0000
        if m_eok:
            total += float(m_eok.group(1).replace(",", "")) * 1_0000_0000
        return total or None
    # 순수 숫자면 그대로 원 (industryCompareInfo의 marketValue는 백만원 단위)
    v = _to_float(s)
    return v


def _naver_market_cap(code: str) -> Optional[float]:
    """네이버 통합정보 marketValue('7조 206억') → 원. 실패 시 None."""
    data = _naver_integration(code)
    if not data:
        return None
    for row in data.get("totalInfos", []):
        if row.get("code") == "marketValue":
            return _parse_kr_amount(row.get("value"))
    return None


def _naver_valuation(code: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(PER, PBR, 배당수익률%) — 실제 키: per/pbr/dividendYieldRatio. '14.92배'/'0.38%' 파싱."""
    data = _naver_integration(code)
    if not data:
        return None, None, None
    vals = {row.get("code"): row.get("value") for row in data.get("totalInfos", [])}
    per = _to_float(vals.get("per"))            # '14.92배' → 14.92
    pbr = _to_float(vals.get("pbr"))            # '1.49배' → 1.49
    dy = _to_float(vals.get("dividendYieldRatio"))  # '0.38%' → 0.38
    return per, pbr, dy


# ---------------------------------------------------------------------------
# 외국인·기관 수급 (네이버) — 일별 순매수 추이
# ---------------------------------------------------------------------------
def _naver_trend(code: str, count: int = 65, max_pages: int = 5) -> Optional[list[dict]]:
    """일별 외국인·기관 순매매. finance.naver.com/item/frgn HTML 파싱.
    [{date, foreign, inst}] 오래된→최신. 한 페이지 ~20일, max_pages까지 (약 3개월)."""
    from bs4 import BeautifulSoup

    out: list[dict] = []
    for page in range(1, max_pages + 1):
        def _call(p=page):
            url = f"https://finance.naver.com/item/frgn.naver?code={code}&page={p}"
            # 이 페이지는 EUC-KR(cp949) 인코딩
            resp = _SESSION.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            resp.encoding = "euc-kr"
            return resp.text

        html = None
        try:
            html = _retry(_call)
        except Exception:
            html = None
        if not html:
            break

        soup = BeautifulSoup(html, "html.parser")
        # 외국인·기관 순매매 표: summary 속성으로 식별
        table = soup.find("table", summary=lambda s: s and "외국인" in s and "기관" in s)
        if table is None:
            # 폴백: 날짜 형식이 있는 행을 가진 표 탐색
            tables = soup.find_all("table")
            table = next((t for t in tables if t.find(string=lambda x: x and "." in str(x)
                          and len(str(x).strip()) == 10 and str(x).strip()[4] == ".")), None)
        if table is None:
            break

        page_rows = 0
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            date_txt = tds[0].get_text(strip=True)  # 'YYYY.MM.DD'
            if not (len(date_txt) == 10 and date_txt[4] == "."):
                continue
            ymd = date_txt.replace(".", "")
            # 표 컬럼: 날짜|종가|전일비|등락률|거래량|기관순매매|외국인순매매|외국인보유주수|외국인보유율
            # 인덱스는 레이아웃에 따라 다를 수 있어, 부호 포함 숫자 셀들을 뒤에서 찾음
            nums = [_to_float(td.get_text(strip=True)) for td in tds]
            # 기관/외국인 순매매는 보통 인덱스 5,6 (음수 가능)
            inst = nums[5] if len(nums) > 5 else None
            foreign = nums[6] if len(nums) > 6 else None
            if foreign is None and inst is None:
                continue
            out.append({"date": ymd, "foreign": foreign or 0.0, "inst": inst or 0.0})
            page_rows += 1
        if page_rows == 0:
            break
        if len(out) >= count:
            break
        time.sleep(0.3)  # 과도한 연속 요청 방지

    if not out:
        # 최후 폴백: integration의 dealTrendInfos(최근 5일)
        integ = _naver_integration(code)
        if integ:
            for r in integ.get("dealTrendInfos", []) or []:
                d = str(r.get("bizdate", "")).replace("-", "")[:8]
                f = _to_float(r.get("foreignerPureBuyQuant"))
                i = _to_float(r.get("organPureBuyQuant"))
                if d and (f is not None or i is not None):
                    out.append({"date": d, "foreign": f or 0.0, "inst": i or 0.0})
        if not out:
            return None

    uniq = {r["date"]: r for r in out}
    series = [uniq[k] for k in sorted(uniq)]
    return series[-count:]  # 최근 count일


def _build_snapshot_from_series(series: list[tuple[str, float]], ticker: str,
                                currency: str = "KRW") -> Optional[StockSnapshot]:
    """(날짜,종가) 시계열 → StockSnapshot (현재가/등락/1개월history/YTD)."""
    if not series:
        return None
    snap = StockSnapshot(ticker=ticker)
    snap.currency = currency
    last_date, last = series[-1]
    snap.price = round(last, 2)
    if len(series) >= 2:
        prev = series[-2][1]
        if prev:
            snap.change_pct = round((last - prev) / prev * 100, 2)
    snap.history = [c for _, c in series[-22:]]   # 최근 약 1개월(거래일)
    year = last_date[:4]
    ytd_series = [c for d, c in series if d[:4] == year]
    if ytd_series:
        first = ytd_series[0]
        if first:
            snap.ytd_pct = round((last - first) / first * 100, 2)
    return snap


def _yf_snapshot(ticker: str) -> StockSnapshot:
    """yfinance 경로 (해외 종목/폴백). 재시도 적용. 항상 StockSnapshot 반환."""
    snap = StockSnapshot(ticker=ticker)
    if yf is None:
        snap.error = "yfinance 미설치"
        return snap
    try:
        def _h():
            h = yf.Ticker(ticker).history(period="1mo")
            return h if (h is not None and not h.empty) else None
        hist = _retry(_h, tries=3)
    except Exception as e:
        snap.error = f"yf: {str(e)[:100]}"
        return snap
    if hist is None or hist.empty:
        snap.error = "yf: 가격 데이터 없음"
        return snap
    hist = hist.dropna(subset=["Close"])
    if hist.empty:
        snap.error = "yf: 가격 데이터 없음"
        return snap
    last = float(hist["Close"].iloc[-1])
    snap.price = round(last, 2)
    if len(hist) >= 2:
        prev = float(hist["Close"].iloc[-2])
        if prev:
            snap.change_pct = round((last - prev) / prev * 100, 2)
    snap.history = [float(x) for x in hist["Close"].tolist()]
    try:
        ytd = yf.Ticker(ticker).history(period="ytd")
        if ytd is not None and not ytd.empty:
            ytd = ytd.dropna(subset=["Close"])
            if not ytd.empty:
                first = float(ytd["Close"].iloc[0])
                if first:
                    snap.ytd_pct = round((last - first) / first * 100, 2)
    except Exception:
        pass
    # 통화 + 시총
    try:
        tk = yf.Ticker(ticker)
        fi = tk.fast_info
        snap.currency = (fi.get("currency", "") or "")
        mc = fi.get("market_cap", None)
        if not mc:
            shares = fi.get("shares", None)
            if shares and snap.price:
                mc = snap.price * float(shares)
        snap.market_cap = float(mc) if mc else None
    except Exception:
        snap.currency = snap.currency or ""
    if snap.market_cap is None:
        try:
            info = yf.Ticker(ticker).get_info()
            if not snap.currency:
                snap.currency = info.get("currency", "") or ""
            mc = info.get("marketCap", None)
            if not mc:
                shares = info.get("sharesOutstanding", None)
                if shares and snap.price:
                    mc = snap.price * float(shares)
            snap.market_cap = float(mc) if mc else None
        except Exception:
            pass
    return snap


def fetch_stock_by_ticker(ticker: str) -> Optional[StockSnapshot]:
    """주가 스냅샷. 한국=네이버 우선·yf 폴백, 해외=yf. 가격 못 받으면 error만 채움."""
    if not ticker:
        return None

    naver_code = _ticker_to_naver_code(ticker)

    if naver_code:
        # 1) 네이버 우선
        try:
            if naver_code == "KOSPI":
                series = _naver_index_prices("KOSPI")
            else:
                series = _naver_daily_prices(naver_code)
            snap = _build_snapshot_from_series(series, ticker, currency="KRW") if series else None
            if snap and snap.price is not None:
                # 종목이면 시총 보강(지수는 시총 없음)
                if naver_code != "KOSPI":
                    mc = _naver_market_cap(naver_code)
                    if mc:
                        snap.market_cap = mc
                    snap.per, snap.pbr, snap.div_yield = _naver_valuation(naver_code)
                return snap
        except Exception as e:
            print(f"  ! 네이버 조회 실패 {ticker}: {str(e)[:80]}", file=sys.stderr)
        # 2) 네이버 실패 → yfinance 폴백
        print(f"  ! {ticker} 네이버 실패 → yfinance 폴백", file=sys.stderr)
        snap = _yf_snapshot(ticker)
        if snap.error and snap.price is None:
            snap.error = "네이버·yfinance 모두 실패"
        return snap

    # 해외 종목 → yfinance
    return _yf_snapshot(ticker)


def fetch_stock(comp: Competitor) -> Optional[StockSnapshot]:
    return fetch_stock_by_ticker(comp.ticker)


# 통화별 KRW 환율 캐시 (USD, JPY, CNY 등 → KRW)
_FX_CACHE: dict[str, Optional[float]] = {"KRW": 1.0}


def get_krw_rate(currency: str) -> Optional[float]:
    """1 단위 외화가 몇 KRW인지 반환. 실패 시 None. yfinance 재시도 적용."""
    if not currency:
        return None
    currency = currency.upper()
    if currency in _FX_CACHE:
        return _FX_CACHE[currency]
    rate = None
    if yf is not None:
        try:
            pair = f"{currency}KRW=X"

            def _h():
                h = yf.Ticker(pair).history(period="5d")
                return h if (h is not None and not h.empty) else None

            hist = _retry(_h, tries=3)
            if hist is not None and not hist.empty:
                hist = hist.dropna(subset=["Close"])
                if not hist.empty:
                    rate = float(hist["Close"].iloc[-1])
        except Exception:
            rate = None
    _FX_CACHE[currency] = rate
    return rate


def market_cap_in_krw(snap: StockSnapshot) -> Optional[float]:
    """스냅샷의 시총을 KRW로 환산. 환율을 못 구하면 None."""
    if not snap or snap.market_cap is None:
        return None
    rate = get_krw_rate(snap.currency or "KRW")
    if rate is None:
        return None
    return snap.market_cap * rate


def format_krw_jo(value_krw: Optional[float]) -> str:
    """KRW 금액을 '○조' 형식으로. 1조 미만은 '○억'으로 보조 표시."""
    if value_krw is None:
        return ""
    jo = value_krw / 1_0000_0000_0000  # 1조 = 10^12
    if jo >= 1:
        return f"{jo:,.1f}조"
    eok = value_krw / 1_0000_0000  # 1억 = 10^8
    return f"{eok:,.0f}억"


def render_sparkline(values: list[float], up: bool, width: int = 140, height: int = 52) -> str:
    """종가 리스트를 작은 SVG 라인 차트로. 기간 내 최저(파랑)·최고(빨강) 점과 값 표시."""
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    lo_i = values.index(lo)
    hi_i = values.index(hi)
    pad_top, pad_bot = 12, 12  # 위/아래 라벨이 잘리지 않도록 충분히
    plot_h = height - pad_top - pad_bot

    def px(i): return i / (n - 1) * (width - 8) + 4
    def py(v): return pad_top + plot_h - (v - lo) / span * plot_h

    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
    color = "#C0392B" if up else "#1B6CC4"
    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="display:block;margin-top:6px;overflow:visible;">',
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>',
    ]
    # 최고점 (빨강) — 점 위쪽 라벨
    hx, hy = px(hi_i), py(hi)
    parts.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="2.2" fill="#C0392B"/>')
    ha = "start" if hi_i < n / 2 else "end"
    parts.append(f'<text x="{hx:.1f}" y="{hy-4:.1f}" text-anchor="{ha}" font-size="9" fill="#C0392B">{hi:,.0f}</text>')
    # 최저점 (파랑) — 점 아래쪽 라벨 (pad_bot 확보로 안 잘림)
    lx, ly = px(lo_i), py(lo)
    parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.2" fill="#1B6CC4"/>')
    la = "start" if lo_i < n / 2 else "end"
    parts.append(f'<text x="{lx:.1f}" y="{ly+10:.1f}" text-anchor="{la}" font-size="9" fill="#1B6CC4">{lo:,.0f}</text>')
    parts.append('</svg>')
    return "".join(parts)


def render_comparison_chart(rows: list[tuple[str, float]]) -> str:
    """(회사명, 연초대비%) 목록을 가로 막대 비교 그래프(SVG)로."""
    if not rows:
        return '<p style="font-size:13px;color:#999;">비교할 데이터가 없습니다.</p>'
    rows = sorted(rows, key=lambda r: r[1], reverse=True)
    max_abs = max((abs(v) for _, v in rows), default=1.0) or 1.0
    row_h, label_w, bar_max, gap = 28, 120, 180, 6
    mid_x = label_w + bar_max + 20  # 0 기준선 위치
    width = mid_x + bar_max + 60
    height = len(rows) * (row_h + gap) + 20
    parts = [f'<svg width="100%" viewBox="0 0 {width} {height}" style="max-width:100%;">']
    parts.append(f'<line x1="{mid_x}" y1="10" x2="{mid_x}" y2="{height-10}" stroke="#ddd" stroke-width="1"/>')
    for i, (name, pct) in enumerate(rows):
        y = 10 + i * (row_h + gap)
        cy = y + row_h / 2
        bar_len = abs(pct) / max_abs * bar_max
        up = pct >= 0
        color = "#C0392B" if up else "#1B6CC4"
        bx = mid_x if up else mid_x - bar_len
        parts.append(f'<text x="{label_w}" y="{cy+4:.0f}" text-anchor="end" font-size="12" fill="#444">{_esc(name)}</text>')
        parts.append(f'<rect x="{bx:.0f}" y="{y}" width="{bar_len:.0f}" height="{row_h}" rx="3" fill="{color}" opacity="0.85"/>')
        sign = "+" if up else ""
        tx = mid_x + bar_len + 6 if up else mid_x - bar_len - 6
        anchor = "start" if up else "end"
        parts.append(f'<text x="{tx:.0f}" y="{cy+4:.0f}" text-anchor="{anchor}" font-size="12" fill="{color}" font-weight="500">{sign}{pct:.1f}%</text>')
    parts.append('</svg>')
    return "".join(parts)


def render_valuation_table(data: list) -> str:
    """한국 종목 밸류에이션 표 (현재가/PER/PBR/배당수익률/시총). PER 오름차순."""
    rows = []
    for d in data:
        s = d.stock
        if not (s and s.price is not None):
            continue
        if not (s.ticker.endswith((".KS", ".KQ"))):
            continue  # 한국 종목만 (밸류에이션 데이터 존재)
        rows.append((d.comp.name, s))
    if not rows:
        return '<p style="font-size:13px;color:#999;">밸류에이션 데이터가 없습니다.</p>'
    rows.sort(key=lambda r: (r[1].per is None, r[1].per or 0))  # PER 오름차순, None은 뒤로

    def cell(v, suffix="", dec=2):
        if v is None:
            return '<td style="padding:8px 10px;color:#bbb;text-align:right;">—</td>'
        return f'<td style="padding:8px 10px;text-align:right;">{v:,.{dec}f}{suffix}</td>'

    head = ('<tr style="background:#F7F6F2;font-size:12px;color:#666;">'
            '<th style="padding:8px 10px;text-align:left;">종목</th>'
            '<th style="padding:8px 10px;text-align:right;">현재가</th>'
            '<th style="padding:8px 10px;text-align:right;">PER</th>'
            '<th style="padding:8px 10px;text-align:right;">PBR</th>'
            '<th style="padding:8px 10px;text-align:right;">배당수익률</th>'
            '<th style="padding:8px 10px;text-align:right;">시총</th></tr>')
    body = ""
    for name, s in rows:
        mc_txt = format_krw_jo(market_cap_in_krw(s))
        body += (f'<tr style="font-size:13px;border-bottom:1px solid #eee;">'
                 f'<td style="padding:8px 10px;">{_esc(name)}</td>'
                 f'<td style="padding:8px 10px;text-align:right;">{s.price:,.0f}</td>'
                 f'{cell(s.per)}{cell(s.pbr)}{cell(s.div_yield, "%")}'
                 f'<td style="padding:8px 10px;text-align:right;">{mc_txt}원</td></tr>')
    return (f'<table style="width:100%;border-collapse:collapse;">{head}{body}</table>'
            f'<p style="font-size:11px;color:#999;margin:8px 0 0;">* PER 오름차순. 해외 종목은 제외.</p>')


def render_flow_chart(trend: list, title: str, width: int = 620, height: int = 200) -> str:
    """외국인·기관 일별 순매수 누적 추이. 초록=외국인, 보라=기관. 날짜 2주 간격 표시."""
    if not trend:
        return '<p style="font-size:13px;color:#999;">수급 데이터가 없습니다.</p>'
    # 누적합으로 경향성 표현
    f_cum, i_cum = [], []
    fs = is_ = 0.0
    for r in trend:
        fs += r["foreign"]; is_ += r["inst"]
        f_cum.append(fs); i_cum.append(is_)
    n = len(trend)
    allv = f_cum + i_cum + [0.0]
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1.0
    pad_l, pad_b = 50, 28
    plot_w, plot_h = width - pad_l - 10, height - pad_b - 16

    def x(i): return pad_l + (i / (n - 1 or 1)) * plot_w
    def y(v): return 12 + plot_h - (v - lo) / span * plot_h

    def line(vals, color):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
        return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>'

    COL_F = "#0F9D58"  # 외국인 = 초록
    COL_I = "#7E57C2"  # 기관 = 보라
    zero_y = y(0)
    parts = [f'<svg width="100%" viewBox="0 0 {width} {height}" style="max-width:100%;">']
    parts.append(f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width-10}" y2="{zero_y:.1f}" stroke="#ddd" stroke-dasharray="3,3"/>')
    parts.append(f'<text x="{pad_l-6}" y="{zero_y+3:.1f}" text-anchor="end" font-size="10" fill="#aaa">0</text>')
    parts.append(line(f_cum, COL_F))
    parts.append(line(i_cum, COL_I))
    # 범례
    parts.append(f'<rect x="{pad_l}" y="2" width="10" height="10" fill="{COL_F}"/><text x="{pad_l+14}" y="11" font-size="11" fill="#666">외국인 누적</text>')
    parts.append(f'<rect x="{pad_l+110}" y="2" width="10" height="10" fill="{COL_I}"/><text x="{pad_l+124}" y="11" font-size="11" fill="#666">기관 누적</text>')
    # 날짜 축: 약 2주(10거래일) 간격으로 눈금+레이블
    tick_y = height - pad_b + 16
    step = 10  # 거래일 기준 약 2주
    shown = set()
    for i in range(0, n, step):
        d = trend[i]["date"]
        xi = x(i)
        parts.append(f'<line x1="{xi:.1f}" y1="{12+plot_h:.1f}" x2="{xi:.1f}" y2="{12+plot_h+4:.1f}" stroke="#ccc"/>')
        anchor = "start" if i == 0 else ("end" if i >= n - step else "middle")
        parts.append(f'<text x="{xi:.1f}" y="{tick_y:.1f}" text-anchor="{anchor}" font-size="10" fill="#aaa">{d[4:6]}/{d[6:8]}</text>')
        shown.add(i)
    # 마지막 날짜가 안 찍혔으면 끝점 추가
    if (n - 1) not in shown:
        d = trend[-1]["date"]
        parts.append(f'<text x="{width-10}" y="{tick_y:.1f}" text-anchor="end" font-size="10" fill="#aaa">{d[4:6]}/{d[6:8]}</text>')
    parts.append('</svg>')
    f_total, i_total = f_cum[-1], i_cum[-1]
    summary = (f'<p style="font-size:12px;color:#666;margin:8px 0 0;">'
               f'기간 누적 순매수 — 외국인 <span style="color:#C0392B;">{f_total:+,.0f}주</span> · '
               f'기관 <span style="color:#1B6CC4;">{i_total:+,.0f}주</span></p>')
    return f'<p style="font-size:13px;font-weight:600;margin:0 0 8px;">{_esc(title)}</p>' + "".join(parts) + summary


# 수급 분석 대상 종목코드 (HD건설기계만)
FLOW_TARGETS = {"267270": "HD건설기계"}


def collect_flow(data: list) -> dict:
    """FLOW_TARGETS에 해당하는 종목의 수급 추이 수집. {code: trend}."""
    out = {}
    for d in data:
        code = d.comp.ticker.split(".")[0] if d.comp.ticker else ""
        if code in FLOW_TARGETS:
            try:
                tr = _naver_trend(code, count=65)  # 약 3개월(거래일)
                if tr:
                    out[code] = tr
            except Exception as e:
                print(f"  ! {d.comp.name} 수급 조회 실패: {str(e)[:60]}", file=sys.stderr)
    return out



# DART 공시 수집
# ---------------------------------------------------------------------------

@dataclass
class Disclosure:
    corp_name: str
    report_nm: str
    rcept_dt: str       # 접수일자 YYYYMMDD
    url: str
    pblntf_ty: str = ""     # 공시 대분류 코드 (A/B/I 등)
    is_important: bool = False  # 중요 공시 강조 여부
    tag: str = ""           # 표시용 태그 (실적/주요사항/수주 등)


# IR 관점 중요 공시 키워드 → 태그 매핑 (report_nm 부분일치)
DISCLOSURE_TAGS = {
    "실적": ["분기보고서", "반기보고서", "사업보고서", "매출액", "영업(잠정)", "손익구조", "실적"],
    "수주·계약": ["공급계약", "수주", "단일판매"],
    "투자·증설": ["유형자산", "신규시설", "투자판단", "타법인주식"],
    "지배구조": ["합병", "분할", "주식교환", "영업양수도"],
    "주주환원": ["자기주식", "배당", "현금ㆍ현물배당"],
    "자금": ["유상증자", "전환사채", "신주인수권", "교환사채", "회사채"],
}


def _classify_disclosure(report_nm: str) -> tuple[str, bool]:
    """공시 제목으로 태그와 중요도 판정."""
    for tag, kws in DISCLOSURE_TAGS.items():
        if any(kw in report_nm for kw in kws):
            return tag, True
    return "기타", False


_DART_CORP_MAP: Optional[dict[str, str]] = None  # 종목코드(6자리) → corp_code(8자리)


def _load_dart_corp_map(api_key: str) -> dict[str, str]:
    """DART 전체 기업 고유번호 목록을 받아 종목코드→corp_code 매핑 생성 (1회)."""
    global _DART_CORP_MAP
    if _DART_CORP_MAP is not None:
        return _DART_CORP_MAP
    mapping: dict[str, str] = {}
    try:
        import io
        import zipfile
        import xml.etree.ElementTree as ET
        url = "https://opendart.fss.or.kr/api/corpCode.xml"
        resp = requests.get(url, params={"crtfc_key": api_key}, timeout=30)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            with z.open(z.namelist()[0]) as f:
                tree = ET.parse(f)
        for item in tree.getroot().findall("list"):
            stock = (item.findtext("stock_code") or "").strip()
            corp = (item.findtext("corp_code") or "").strip()
            if stock and corp:
                mapping[stock] = corp
    except Exception as e:
        print(f"  ! DART 기업코드 목록 로드 실패: {e}", file=sys.stderr)
    _DART_CORP_MAP = mapping
    return mapping


def fetch_disclosures(comp: Competitor, cfg: Config, api_key: str) -> list[Disclosure]:
    """국내 상장사(.KS/.KQ)의 최근 공시를 DART에서 조회. 중요 유형만, 별도 기간 사용."""
    if not comp.ticker.endswith((".KS", ".KQ")):
        return []  # 해외 종목은 DART 대상 아님
    stock_code = comp.ticker.split(".")[0]
    corp_map = _load_dart_corp_map(api_key)
    corp_code = corp_map.get(stock_code)
    if not corp_code:
        print(f"  ! {comp.name}({stock_code}) DART corp_code 매핑 실패", file=sys.stderr)
        return []
    end = dt.datetime.now()
    bgn = end - dt.timedelta(days=cfg.disclosure_lookback_days)
    out: list[Disclosure] = []
    # 공시 유형별로 조회 (A=정기, B=주요사항, I=거래소공시 등)
    for ty in (cfg.disclosure_types or [""]):
        try:
            params = {
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bgn_de": bgn.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "page_count": 100,
            }
            if ty:
                params["pblntf_ty"] = ty
            resp = requests.get("https://opendart.fss.or.kr/api/list.json", params=params, timeout=20)
            data = resp.json()
            if data.get("status") != "000":
                continue  # 해당 유형 공시 없음(013) 등은 조용히 넘어감
            for it in data.get("list", []):
                rcept_no = it.get("rcept_no", "")
                report_nm = it.get("report_nm", "")
                tag, important = _classify_disclosure(report_nm)
                out.append(Disclosure(
                    corp_name=it.get("corp_name", comp.name),
                    report_nm=report_nm,
                    rcept_dt=it.get("rcept_dt", ""),
                    url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                    pblntf_ty=ty,
                    is_important=important,
                    tag=tag,
                ))
        except Exception as e:
            print(f"  ! {comp.name} 공시 조회 실패({ty}): {str(e)[:60]}", file=sys.stderr)
            continue
    # 중복 제거(접수번호 기준) + 최신순 + 회사별 상한
    seen = set()
    uniq = []
    for d in sorted(out, key=lambda x: x.rcept_dt, reverse=True):
        if d.url in seen:
            continue
        seen.add(d.url)
        uniq.append(d)
    return uniq[:cfg.disclosure_max_per_company]


CACHE_IR = "cache/ir_updates.json"
CACHE_HISTORY = "cache/update_history.json"


def collect_ir_updates(cfg: Config, data: list["CompetitorData"],
                       disclosures: list) -> tuple[list, list, dict]:
    """경쟁사별 IR 자료를 수집하고 변경 감지.
    소스: config의 ir_sources(RSS/HTML) + SEC EDGAR(cik 있으면) + DART 공시.
    반환: (현재 최신 IRUpdate 전체, 변경분만, 히스토리)."""
    cache = irm.load_cache(CACHE_IR)

    # DART 공시를 회사별 IRUpdate로 변환해두기 (회사명 → IRUpdate 리스트)
    disc_by_comp: dict[str, list] = {}
    for d in disclosures:
        ir_item = irm.IRUpdate(
            company=d.corp_name, title=d.report_nm, link=d.url,
            published=irm._from_iso(None), source_type="dart",
        )
        # 접수일자(YYYYMMDD) → datetime
        if d.rcept_dt and len(d.rcept_dt) == 8:
            try:
                ir_item.published = dt.datetime.strptime(d.rcept_dt, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                pass
        disc_by_comp.setdefault(d.corp_name, []).append(ir_item)

    all_current: list = []
    all_changes: list = []

    for d in data:
        comp = d.comp
        fresh: list = []
        # 1) config의 ir_sources (사용자 검증 URL 최우선)
        if comp.ir_sources:
            fresh.extend(irm.fetch_ir_updates(comp.name, comp.ir_sources))
        else:
            # Auto Discovery는 로그용으로만 (운영은 config 우선)
            if comp.ticker:
                pass  # 홈페이지를 모르면 생략. 필요 시 config에 homepage 추가 가능
        # 2) SEC EDGAR (미국 상장사)
        if comp.cik:
            fresh.extend(irm.fetch_sec_filings(comp.name, comp.cik))
        # 3) DART 공시 (회사명 매칭)
        for cn, items in disc_by_comp.items():
            if cn == comp.name or cn in comp.name or comp.name in cn:
                fresh.extend(items)

        if not fresh:
            continue
        fresh = irm._dedupe(fresh)
        detected, cache = irm.detect_changes(comp.name, fresh, cache)
        # 현재 최신 목록(REMOVED 제외) + 변경분 분리
        for it in detected:
            if it.is_removed:
                all_changes.append(it)
            else:
                all_current.append(it)
                if it.is_new or it.is_updated:
                    all_changes.append(it)

    irm.save_cache(CACHE_IR, cache)
    history = irm.update_history(CACHE_HISTORY, all_changes)
    return all_current, all_changes, history


def collect_disclosures(cfg: Config, data: list["CompetitorData"]) -> list[Disclosure]:
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        print("  ! DART_API_KEY 없음 — 공시 수집 생략", file=sys.stderr)
        return []
    all_disc: list[Disclosure] = []
    for d in data:
        all_disc.extend(fetch_disclosures(d.comp, cfg, api_key))
    # 최신순 정렬
    all_disc.sort(key=lambda x: x.rcept_dt, reverse=True)
    return all_disc


@dataclass
class CompetitorData:
    comp: Competitor
    news: list[NewsItem]
    stock: Optional[StockSnapshot]


def collect_all(cfg: Config) -> list[CompetitorData]:
    return collect_for(cfg, cfg.competitors)


def collect_for(cfg: Config, comps: list[Competitor]) -> list[CompetitorData]:
    results: list[CompetitorData] = []
    for comp in comps:
        print(f"  - {comp.name} 수집 중...", file=sys.stderr)
        stock = fetch_stock(comp)
        if stock and stock.price is None and stock.error:
            print(f"    · 주가 실패: {comp.name} ({stock.error})", file=sys.stderr)
        results.append(CompetitorData(comp=comp, news=fetch_news(comp, cfg), stock=stock))
    return results


CATEGORY_COLORS = {
    "실적": ("#FCEBEB", "#A32D2D"),
    "공시": ("#E6F1FB", "#185FA5"),
    "리포트": ("#FAEEDA", "#854F0B"),
    "뉴스": ("#F1EFE8", "#5F5E5A"),
    "시장": ("#EDE9FB", "#4A3F9E"),
    "IR": ("#E6F1FB", "#185FA5"),
    "ESG": ("#E1F5EE", "#0F6E56"),
    "주주환원": ("#FAEEDA", "#854F0B"),
    "전략": ("#EDE9FB", "#4A3F9E"),
}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tag_badges(tags: list) -> str:
    out = ""
    for t in (tags or []):
        bg, fg = CATEGORY_COLORS.get(t, CATEGORY_COLORS["뉴스"])
        out += f'<span style="font-size:10px;background:{bg};color:{fg};padding:1px 6px;border-radius:6px;margin-left:4px;">{_esc(t)}</span>'
    return out


def _state_badge(it) -> str:
    if getattr(it, "is_new", False):
        return '<span style="font-size:10px;background:#FCEBEB;color:#C0392B;padding:1px 7px;border-radius:6px;font-weight:600;">NEW</span>'
    if getattr(it, "is_updated", False):
        return '<span style="font-size:10px;background:#FAEEDA;color:#854F0B;padding:1px 7px;border-radius:6px;font-weight:600;">UPDATED</span>'
    if getattr(it, "is_removed", False):
        return '<span style="font-size:10px;background:#EEE;color:#888;padding:1px 7px;border-radius:6px;font-weight:600;">REMOVED</span>'
    return ""


def _ir_line(it) -> str:
    date_txt = ""
    if it.published is not None:
        kst = it.published + dt.timedelta(hours=9)
        date_txt = f'<span style="color:#999;"> · {kst:%Y-%m-%d}</span>'
    src = f'<span style="color:#bbb;font-size:11px;"> [{_esc(it.source_type)}]</span>'
    return (
        f'<li style="margin-bottom:8px;">{_state_badge(it)} '
        f'<a href="{_esc(it.link)}" style="color:#185FA5;text-decoration:none;font-size:13px;">{_esc(it.title)}</a>'
        f'{_tag_badges(it.tags)}{date_txt}{src}</li>'
    )


def _render_oneline_summary(changes: list, disclosures, kospi_snap, now) -> str:
    """최상단 얇은 한 줄 상태 표시줄."""
    parts = []
    # 신규 변경 건수
    n_changes = len(changes or [])
    if n_changes:
        parts.append(f'<span style="color:#C0392B;font-weight:600;">🔴 신규 {n_changes}건</span>')
    else:
        parts.append('<span style="color:#0F6E56;font-weight:600;">🟢 신규 없음</span>')
    # 신규 공시 건수
    if disclosures:
        n_imp = sum(1 for d in disclosures if getattr(d, "is_important", False))
        parts.append(f'<span style="color:#666;">공시 {len(disclosures)}건' + (f' (중요 {n_imp})' if n_imp else '') + '</span>')
    # 코스피
    if kospi_snap and kospi_snap.price is not None:
        chg = kospi_snap.change_pct
        col = "#C0392B" if (chg or 0) >= 0 else "#1B6CC4"
        arr = "▲" if (chg or 0) >= 0 else "▼"
        chg_txt = f'{arr} {abs(chg):.2f}%' if chg is not None else ''
        parts.append(f'<span style="color:#666;">코스피 {kospi_snap.price:,.0f} <span style="color:{col};">{chg_txt}</span></span>')
    parts.append(f'<span style="color:#999;">갱신 {now:%H:%M} KST</span>')
    sep = '<span style="color:#ddd;margin:0 8px;">·</span>'
    return ('<div style="padding:10px 14px;background:#F7F6F2;border-radius:8px;font-size:13px;">'
            + sep.join(parts) + '</div>')


def _render_change_dashboard(changes: list) -> str:
    """(미사용) 이전 최상단 대시보드 — 호환을 위해 유지."""
    if not changes:
        return ('<div style="padding:14px 18px;background:#E1F5EE;border-radius:8px;">'
                '<span style="font-size:15px;color:#0F6E56;font-weight:600;">🟢 신규 업데이트 없음</span></div>')
    n_new = sum(1 for c in changes if c.is_new)
    n_upd = sum(1 for c in changes if c.is_updated)
    n_rem = sum(1 for c in changes if c.is_removed)
    head = (f'<span style="font-size:15px;color:#C0392B;font-weight:600;">🔴 신규 업데이트 {len(changes)}건</span>'
            f'<span style="font-size:12px;color:#888;margin-left:8px;">NEW {n_new} · UPDATED {n_upd} · REMOVED {n_rem}</span>')
    rows = ""
    for c in changes[:30]:
        rows += (f'<div style="margin:8px 0;">{_state_badge(c)} '
                 f'<span style="font-size:12px;color:#666;">{_esc(c.company)}</span> '
                 f'<a href="{_esc(c.link)}" style="color:#185FA5;text-decoration:none;font-size:13px;">{_esc(c.title)}</a>'
                 f'{_tag_badges(c.tags)}</div>')
    return (f'<div style="padding:14px 18px;background:#FCF4F2;border-radius:8px;border:1px solid #F0D9D4;">'
            f'{head}<div style="margin-top:10px;">{rows}</div></div>')


def _render_ir_updates(current: list) -> str:
    """IR Updates 탭: 회사별 최신 자료 (회사당 최대 10건)."""
    if not current:
        return '<p style="font-size:13px;color:#999;">수집된 IR 자료가 없습니다. config의 ir_sources 또는 cik를 확인하세요.</p>'
    by_comp: dict = {}
    for it in current:
        by_comp.setdefault(it.company, []).append(it)
    out = ""
    for comp, items in by_comp.items():
        items = sorted(items, key=lambda x: x.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)[:10]
        links = "".join(_ir_line(it) for it in items)
        out += (f'<p style="font-size:13px;font-weight:600;margin:14px 0 4px;">{_esc(comp)} '
                f'<span style="font-weight:400;color:#999;font-size:11px;">최신 {len(items)}건</span></p>'
                f'<ul style="margin:0 0 8px;padding-left:18px;line-height:1.6;">{links}</ul>')
    return out


def _render_history(history: list) -> str:
    """업데이트 히스토리 탭: 최근 30일 변경 이력."""
    if not history:
        return '<p style="font-size:13px;color:#999;">최근 30일 내 변경 이력이 없습니다.</p>'
    hist = sorted(history, key=lambda h: h.get("detected_at", ""), reverse=True)
    rows = ""
    for h in hist[:100]:
        st = h.get("state", "")
        color = {"NEW": "#C0392B", "UPDATED": "#854F0B", "REMOVED": "#888"}.get(st, "#666")
        dtxt = ""
        d = irm._from_iso(h.get("detected_at"))
        if d:
            kst = d + dt.timedelta(hours=9)
            dtxt = f'<span style="color:#999;font-size:11px;"> · {kst:%m-%d %H:%M}</span>'
        rows += (f'<li style="margin-bottom:6px;">'
                 f'<span style="font-size:10px;color:{color};font-weight:600;">{st}</span> '
                 f'<span style="font-size:12px;color:#666;">{_esc(h.get("company",""))}</span> '
                 f'<a href="{_esc(h.get("link","#"))}" style="color:#185FA5;text-decoration:none;font-size:13px;">{_esc(h.get("title",""))}</a>'
                 f'{dtxt}</li>')
    return f'<ul style="margin:0;padding-left:18px;line-height:1.5;">{rows}</ul>'


def _render_link(n: NewsItem) -> str:
    date_txt = ""
    if n.published is not None:
        kst = n.published + dt.timedelta(hours=9)
        date_txt = f'<span style="color:#999;"> · {kst:%Y-%m-%d}</span>'
    return (
        f'<li><a href="{_esc(n.link)}" style="color:#185FA5;text-decoration:none;">{_esc(n.title)}</a>'
        f' <span style="color:#999;">— {_esc(n.source)}</span>{date_txt}</li>'
    )


def _render_source_links(blocks: list[tuple[str, list[NewsItem]]]) -> str:
    out = ""
    for name, news in blocks:
        if not news:
            continue
        news_sorted = sorted(news, key=lambda n: n.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
        links = "".join(_render_link(n) for n in news_sorted)
        out += f'<p style="font-size:13px;font-weight:500;margin:10px 0 4px;">{_esc(name)}</p><ul style="margin:0 0 8px;padding-left:18px;font-size:12px;line-height:1.6;">{links}</ul>'
    return out or '<p style="font-size:12px;color:#999;">수집된 뉴스 없음</p>'


def _render_disclosures(disclosures: Optional[list], new_links: Optional[set] = None) -> str:
    new_links = new_links or set()
    if disclosures is None:
        return '<p style="font-size:13px;color:#999;">DART_API_KEY가 설정되지 않아 공시를 수집하지 않았습니다.</p>'
    if not disclosures:
        return '<p style="font-size:13px;color:#999;">해당 기간 내 공시가 없습니다.</p>'

    # 회사별 그룹핑
    by_comp: dict = {}
    for d in disclosures:
        by_comp.setdefault(d.corp_name, []).append(d)

    # 중요 공시가 있는 회사를 위로
    def comp_sort_key(item):
        name, items = item
        return (-sum(1 for x in items if x.is_important), name)

    out = ""
    for name, items in sorted(by_comp.items(), key=comp_sort_key):
        items = sorted(items, key=lambda x: (not x.is_important, x.rcept_dt and -int(x.rcept_dt or 0)))
        n_imp = sum(1 for x in items if x.is_important)
        head = (f'<p style="font-size:13px;font-weight:600;margin:14px 0 4px;">{_esc(name)} '
                f'<span style="font-weight:400;color:#999;font-size:11px;">{len(items)}건'
                f'{" · 중요 " + str(n_imp) + "건" if n_imp else ""}</span></p>')
        rows = ""
        for d in items:
            dt_txt = ""
            if d.rcept_dt and len(d.rcept_dt) == 8:
                dt_txt = f"{d.rcept_dt[:4]}-{d.rcept_dt[4:6]}-{d.rcept_dt[6:]}"
            tag_badge = ""
            if d.is_important and d.tag and d.tag != "기타":
                bg, fg = CATEGORY_COLORS.get("실적" if d.tag == "실적" else "전략", ("#FCEBEB", "#C0392B"))
                tag_badge = f'<span style="font-size:10px;background:{bg};color:{fg};padding:1px 6px;border-radius:6px;margin-right:4px;">{_esc(d.tag)}</span>'
            new_badge = ""
            if d.url in new_links:
                new_badge = '<span style="font-size:10px;background:#FCEBEB;color:#C0392B;padding:1px 6px;border-radius:6px;font-weight:600;margin-right:4px;">NEW</span>'
            star = '🔴 ' if d.is_important else ''
            weight = "600" if d.is_important else "400"
            rows += (
                f'<li style="margin-bottom:6px;">{new_badge}{star}{tag_badge}'
                f'<a href="{_esc(d.url)}" style="font-size:13px;color:#185FA5;text-decoration:none;font-weight:{weight};">{_esc(d.report_nm)}</a>'
                f' <span style="color:#999;font-size:12px;">· {dt_txt}</span></li>'
            )
        out += head + f'<ul style="margin:0 0 8px;padding-left:18px;line-height:1.5;">{rows}</ul>'
    return out


def _render_stock_card(d) -> str:
    """기업 한 곳의 주가 스냅샷 카드. 가격을 못 받았으면 '데이터 없음'으로 표시."""
    name = _esc(d.comp.name)
    # 가격 수집 실패 시: 카드를 숨기지 않고 '데이터 없음' 표시 (조용히 사라지는 것 방지)
    if not (d.stock and d.stock.price is not None):
        return f"""
        <div style="background:#F7F6F2;border-radius:8px;padding:12px;opacity:0.6;">
          <p style="font-size:12px;color:#5F5E5A;margin:0;">{name}</p>
          <p style="font-size:14px;color:#aaa;margin:6px 0 0;">데이터 없음</p>
        </div>"""
    chg = d.stock.change_pct
    color = "#C0392B" if (chg or 0) >= 0 else "#1B6CC4"
    arrow = "▲" if (chg or 0) >= 0 else "▼"
    chg_txt = f"{arrow} {abs(chg):.2f}%" if chg is not None else "—"
    mc_txt = format_krw_jo(market_cap_in_krw(d.stock))
    mc_line = f'<p style="font-size:11px;color:#888;margin:4px 0 0;">시총 {mc_txt} 원</p>' if mc_txt else ''
    spark = render_sparkline(d.stock.history, (chg or 0) >= 0) if d.stock.history else ''
    return f"""
        <div style="background:#F7F6F2;border-radius:8px;padding:12px;">
          <p style="font-size:12px;color:#5F5E5A;margin:0;">{name}</p>
          <p style="font-size:18px;font-weight:500;margin:2px 0;">{d.stock.price:,} <span style="font-size:12px;color:#888;">{_esc(d.stock.currency)}</span></p>
          <p style="font-size:12px;color:{color};margin:0;">{chg_txt}</p>
          {mc_line}
          {spark}
        </div>"""


def render_html(cfg: Config, data: list[CompetitorData],
                kospi_snap: Optional[StockSnapshot], kospi_news: list[NewsItem],
                disclosures: Optional[list] = None,
                group_data: Optional[list] = None,
                ir_current: Optional[list] = None,
                ir_changes: Optional[list] = None,
                history: Optional[list] = None,
                flows: Optional[dict] = None) -> str:
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)
    total_news = sum(len(d.news) for d in data) + len(kospi_news)
    ir_current = ir_current or []
    ir_changes = ir_changes or []
    history = history or []
    disclosures = disclosures if disclosures is not None else None

    # 코스피 지수 카드 (보라색 강조)
    kospi_card = ""
    if kospi_snap and kospi_snap.price is not None:
        chg = kospi_snap.change_pct
        color = "#C0392B" if (chg or 0) >= 0 else "#1B6CC4"
        arrow = "▲" if (chg or 0) >= 0 else "▼"
        chg_txt = f"{arrow} {abs(chg):.2f}%" if chg is not None else "—"
        kospi_card = f"""
        <div style="background:#EDE9FB;border:1px solid #C9BEF2;border-radius:8px;padding:12px;">
          <p style="font-size:12px;color:#4A3F9E;margin:0;font-weight:600;">📊 코스피 (KOSPI)</p>
          <p style="font-size:18px;font-weight:500;margin:2px 0;">{kospi_snap.price:,.2f}</p>
          <p style="font-size:12px;color:{color};margin:0;">{chg_txt}</p>
        </div>"""
    else:
        # 코스피도 실패하면 표시
        kospi_card = """
        <div style="background:#EDE9FB;border:1px solid #C9BEF2;border-radius:8px;padding:12px;opacity:0.6;">
          <p style="font-size:12px;color:#4A3F9E;margin:0;font-weight:600;">📊 코스피 (KOSPI)</p>
          <p style="font-size:14px;color:#aaa;margin:6px 0 0;">데이터 없음</p>
        </div>"""

    # 건설기계 기업 카드들 — 국내 우선 정렬
    priority = {"HD건설기계": 0, "두산밥캣": 1, "진성티이씨": 2}
    ordered = sorted(data, key=lambda d: priority.get(d.comp.name, 99))
    stock_cards = "".join(_render_stock_card(d) for d in ordered)

    # 그룹주 카드들
    group_data = group_data or []
    group_cards = "".join(_render_stock_card(d) for d in group_data)
    group_blocks = [(d.comp.name, d.news) for d in group_data]
    has_group = bool(group_cards)

    # 연초 대비 비교 그래프 데이터
    comp_rows = []
    if kospi_snap and kospi_snap.ytd_pct is not None:
        comp_rows.append(("코스피", kospi_snap.ytd_pct))
    for d in ordered:
        if d.stock and d.stock.ytd_pct is not None:
            comp_rows.append((d.comp.name, d.stock.ytd_pct))
    comparison_chart = render_comparison_chart(comp_rows)

    # 밸류에이션 표 (건설기계 + 그룹주 한국 종목 전체)
    valuation_table = render_valuation_table(list(data) + list(group_data))

    # 수급 그래프 (HD건설기계만)
    flows = flows or {}
    flow_html = ""
    for code, name in FLOW_TARGETS.items():
        if code in flows:
            flow_html += render_flow_chart(flows[code], f"{name} 외국인·기관 순매수 추이 (누적·주)")
    if not flow_html:
        flow_html = '<p style="font-size:13px;color:#999;">수급 데이터가 없습니다.</p>'
    has_flow = bool(flows)
    flow_tab_btn = '<button class="tab-btn" onclick="showTab(\'tab-flow\')">외국인·기관 수급</button>'
    flow_panel = f'''<div id="tab-flow" class="tab-panel">
        <p style="font-size:13px;color:#666;margin:0 0 12px;">HD건설기계의 외국인·기관 순매수 추이입니다. 페이지 갱신 시 함께 업데이트됩니다.</p>
        {flow_html}
      </div>'''

    comp_blocks = [(d.comp.name, d.news) for d in data]
    group_tab_btn = ('<button class="tab-btn" onclick="showTab(\'tab-group\')">그룹주 뉴스</button>'
                     if has_group else '')
    group_total_krw = sum(
        (market_cap_in_krw(d.stock) or 0)
        for d in group_data if d.stock and d.stock.price is not None
    )
    group_total_txt = (f'<span style="font-weight:700;font-size:16px;color:#1a1a1a;'
                       f'background:#FFE680;padding:2px 8px;border-radius:6px;margin-left:10px;">'
                       f'총 시총 {format_krw_jo(group_total_krw)}원</span>'
                       if group_total_krw else '')
    group_snapshot = (f'''<div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <p style="font-size:13px;font-weight:600;color:#666;margin:0 0 12px;">그룹주 주가 스냅샷 (HD현대 그룹){group_total_txt}<span style="font-weight:400;color:#aaa;font-size:11px;display:block;margin-top:4px;">차트는 최근 약 1개월 일별 종가 흐름</span></p>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;">{group_cards}</div>
    </div>''' if has_group else '')
    group_panel = (f'''<div id="tab-group" class="tab-panel">{_render_source_links(group_blocks)}</div>'''
                   if has_group else '')

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<meta name="data-version" content="{now:%Y%m%d%H%M%S}">
<title>{_esc(cfg.report_title)}</title>
<style>
  .tab-btn {{ padding:9px 12px; border:none; background:#EFEDE7; cursor:pointer; font-size:13px; font-weight:500; color:#666; border-radius:8px 8px 0 0; }}
  .tab-btn.active {{ background:#fff; color:#1a1a1a; box-shadow:inset 0 -2px 0 #4A3F9E; }}
  .tab-panel {{ display:none; }}
  .tab-panel.active {{ display:block; }}
</style>
</head>
<body style="margin:0;background:#EFEDE7;font-family:-apple-system,'Segoe UI',sans-serif;color:#1a1a1a;">
<div style="max-width:680px;margin:0 auto;padding:20px;">
  <div style="background:#fff;border-radius:12px;border:1px solid #e3e1d9;overflow:hidden;">
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:center;">
        <span style="font-size:18px;font-weight:600;">📡 {_esc(cfg.report_title)}</span>
        <span style="font-size:13px;color:#777;">갱신 {now:%Y년 %m월 %d일} {now:%H:%M} KST</span>
      </div>
      <p style="font-size:13px;color:#999;margin:8px 0 0;">건설기계 {len(cfg.competitors)}개사 + 코스피 · 수집기간 {(now - dt.timedelta(hours=cfg.news_lookback_hours)):%m월 %d일 %H시} ~ {now:%m월 %d일 %H시} · 뉴스 {total_news}건</p>
    </div>

    <!-- 주가 스냅샷 (건설기계) -->
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <p style="font-size:13px;font-weight:600;color:#666;margin:0 0 12px;">주가 스냅샷 (건설기계) <span style="font-weight:400;color:#aaa;font-size:11px;">· 차트는 최근 약 1개월 일별 종가 흐름</span></p>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;">
        {kospi_card}
        {stock_cards}
      </div>
    </div>

    {group_snapshot}

    <!-- 탭 -->
    <div style="padding:20px 24px;">
      <div style="display:flex;gap:4px;margin-bottom:16px;flex-wrap:wrap;">
        <button class="tab-btn active" onclick="showTab('tab-dart')">공시 (DART)</button>
        <button class="tab-btn" onclick="showTab('tab-comp')">경쟁사 뉴스</button>
        <button class="tab-btn" onclick="showTab('tab-kospi')">코스피 뉴스</button>
        {group_tab_btn}
        <button class="tab-btn" onclick="showTab('tab-ytd')">연초 대비 비교</button>
        <button class="tab-btn" onclick="showTab('tab-val')">밸류에이션</button>
        {flow_tab_btn}
      </div>

      <div id="tab-dart" class="tab-panel active">
        {_render_disclosures(disclosures, {c.link for c in (ir_changes or []) if (c.is_new or c.is_updated) and getattr(c, 'source_type', '') == 'dart'})}
      </div>

      <div id="tab-comp" class="tab-panel">
        {_render_source_links(comp_blocks)}
      </div>

      <div id="tab-kospi" class="tab-panel">
        {_render_source_links([("코스피", kospi_news)])}
      </div>

      {group_panel}

      <div id="tab-ytd" class="tab-panel">
        <p style="font-size:13px;color:#666;margin:0 0 12px;">코스피와 각 기업의 올해 누적 주가 수익률 비교입니다.</p>
        {comparison_chart}
      </div>

      <div id="tab-val" class="tab-panel">
        <p style="font-size:13px;color:#666;margin:0 0 12px;">국내 종목 밸류에이션 비교 (네이버 금융 기준).</p>
        {valuation_table}
      </div>

      {flow_panel}
    </div>
  </div>
  <p style="font-size:11px;color:#999;text-align:center;margin:12px 0 0;">자동 생성 리포트 · 원문 확인 권장 · 이 화면은 약 10분 단위로 갱신된 값을 보여줍니다(실시간 시세 아님). 데이터 기준 시각은 상단 ‘갱신 시각’ 참고.</p>
</div>
<script>
function showTab(id) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}

// 데이터가 새로 올라오면 자동 새로고침 (고정 주기 X, 새 데이터 감지 시에만)
(function() {{
  var meta = document.querySelector('meta[name="data-version"]');
  var current = meta ? meta.getAttribute('content') : null;
  if (!current) return;
  function check() {{
    // 페이지 자신을 캐시 우회로 가볍게 다시 받아 data-version만 비교
    fetch(window.location.pathname + '?_=' + Date.now(), {{ cache: 'no-store' }})
      .then(function(r) {{ return r.text(); }})
      .then(function(html) {{
        var m = html.match(/name="data-version" content="(\\d+)"/);
        if (m && m[1] !== current) {{
          window.location.reload();
        }}
      }})
      .catch(function() {{}});  // 네트워크 오류는 조용히 무시
  }}
  setInterval(check, 60000);  // 1분마다 확인
}})();
</script>
</body></html>"""


def _render_changes_email(ir_changes: list, disclosures: list, data: list) -> str:
    """이메일 본문: 변경분만 (신규/수정/삭제 IR + 신규 공시 + 신규 뉴스)."""
    parts = ['<div style="font-family:sans-serif;max-width:600px;">']
    if ir_changes:
        parts.append('<h3 style="color:#C0392B;">IR 자료 변경</h3><ul>')
        for c in ir_changes[:40]:
            st = "NEW" if c.is_new else ("UPDATED" if c.is_updated else "REMOVED")
            parts.append(f'<li>[{st}] {_esc(c.company)} — <a href="{_esc(c.link)}">{_esc(c.title)}</a></li>')
        parts.append('</ul>')
    if disclosures:
        parts.append('<h3 style="color:#185FA5;">신규 공시 (DART)</h3><ul>')
        for d in disclosures[:20]:
            parts.append(f'<li>{_esc(d.corp_name)} — <a href="{_esc(d.url)}">{_esc(d.report_nm)}</a></li>')
        parts.append('</ul>')
    if not ir_changes and not disclosures:
        parts.append('<p style="color:#0F6E56;font-weight:600;">신규 업데이트 없음</p>')
    parts.append('</div>')
    return "".join(parts)


def send_email(cfg: Config, html: str, has_changes: bool) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    if not (cfg.smtp_host and cfg.mail_from and cfg.mail_to):
        raise RuntimeError("SMTP 설정이 비어 있습니다.")
    subj_tag = "신규 업데이트" if has_changes else "변경 없음"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{subj_tag}] {cfg.report_title} — {dt.datetime.now():%Y-%m-%d}"
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.mail_to)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
        server.starttls()
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.sendmail(cfg.mail_from, cfg.mail_to, msg.as_string())
    print(f"  메일 발송 완료 → {', '.join(cfg.mail_to)}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="경쟁사 IR 변경 감지 모니터")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="report.html")
    args = parser.parse_args()

    cfg = load_config(args.config)
    print(f"[1/4] 설정 로드 — 건설기계 {len(cfg.competitors)}개사 + 코스피", file=sys.stderr)

    print("[2/4] 데이터 수집 중...", file=sys.stderr)
    data = collect_all(cfg)
    print("  - 코스피 지수·뉴스 수집 중...", file=sys.stderr)
    kospi_snap = fetch_stock_by_ticker(cfg.kospi_ticker)
    kospi_news = fetch_kospi_news(cfg)
    print("  - 그룹주 수집 중...", file=sys.stderr)
    group_data = collect_for(cfg, cfg.group_stocks)

    # 주가 수집 상태 요약 (디버깅에 도움)
    all_stock = [d.stock for d in (data + group_data) if d.stock] + ([kospi_snap] if kospi_snap else [])
    ok = sum(1 for s in all_stock if s and s.price is not None)
    fail = len(all_stock) - ok
    print(f"  → 주가 수집 결과: 성공 {ok} / 실패 {fail}", file=sys.stderr)
    if fail and ok == 0:
        # 전부 실패면 명확히 경고 (워크플로우 로그에서 눈에 띄게)
        print("  !! 경고: 모든 주가 수집 실패 — 소스 차단 또는 네트워크 문제 가능", file=sys.stderr)

    if args.dry_run:
        print("\n=== DRY RUN ===")
        if kospi_snap:
            print(f"[코스피] {kospi_snap.price} ({kospi_snap.change_pct}) err={kospi_snap.error}")
        for d in data + group_data:
            st = d.stock
            print(f"[{d.comp.name}] 주가={st.price if st else None} "
                  f"YTD={st.ytd_pct if st else None} 뉴스={len(d.news)}건 "
                  f"{'err=' + st.error if st and st.error else ''}")
        return

    print("[3/4] 공시 수집 + IR 변경 감지 중...", file=sys.stderr)
    disclosures = collect_disclosures(cfg, data + group_data)
    ir_current, ir_changes, history = collect_ir_updates(cfg, data + group_data, disclosures)
    print(f"  - IR 자료 {len(ir_current)}건, 변경 {len(ir_changes)}건 감지", file=sys.stderr)

    print("[4/4] 수급 수집 + 리포트 렌더링...", file=sys.stderr)
    flows = collect_flow(data + group_data)
    html = render_html(cfg, data, kospi_snap, kospi_news,
                       disclosures, group_data, ir_current, ir_changes, history,
                       flows=flows)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  저장 완료 → {args.out}", file=sys.stderr)

    if args.send:
        has_changes = bool(ir_changes or disclosures)
        email_html = _render_changes_email(ir_changes, disclosures or [], data)
        send_email(cfg, email_html, has_changes)


if __name__ == "__main__":
    main()
