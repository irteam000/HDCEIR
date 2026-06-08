"""
경쟁사 모니터링 자동화 MVP (코스피 트랙 + 건설기계 트랙)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import feedparser
import requests
import yaml

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
        )
        for c in raw["competitors"]
    ]
    kospi = raw.get("kospi", {})
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
    # 코스피 관련 뉴스는 한국어로 더 많이 모이도록 넉넉히 수집
    return _fetch_news_by_keywords(cfg.kospi_keywords, "ko", "KR",
                                   cfg.news_lookback_hours, max(cfg.max_news_per_competitor, 6))


# 통화별 KRW 환율 캐시 (USD, JPY, CNY 등 → KRW)
_FX_CACHE: dict[str, Optional[float]] = {"KRW": 1.0}


def get_krw_rate(currency: str) -> Optional[float]:
    """1 단위 외화가 몇 KRW인지 반환. 실패 시 None."""
    if not currency:
        return None
    currency = currency.upper()
    if currency in _FX_CACHE:
        return _FX_CACHE[currency]
    rate = None
    if yf is not None:
        try:
            pair = f"{currency}KRW=X"
            hist = yf.Ticker(pair).history(period="5d")
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


def fetch_stock_by_ticker(ticker: str) -> Optional[StockSnapshot]:
    if not ticker:
        return None
    snap = StockSnapshot(ticker=ticker)
    if yf is None:
        snap.error = "yfinance 미설치"
        return snap
    try:
        hist = yf.Ticker(ticker).history(period="1mo")
        if hist is not None and not hist.empty:
            hist = hist.dropna(subset=["Close"])
        if hist is None or hist.empty:
            snap.error = "가격 데이터 없음"
            return snap
        last = float(hist["Close"].iloc[-1])
        snap.price = round(last, 2)
        if len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            if prev:
                snap.change_pct = round((last - prev) / prev * 100, 2)
        try:
            fi = yf.Ticker(ticker).fast_info
            snap.currency = fi.get("currency", "") or ""
            mc = fi.get("market_cap", None)
            snap.market_cap = float(mc) if mc else None
        except Exception:
            snap.currency = ""
    except Exception as e:
        snap.error = str(e)[:120]
    return snap



def fetch_stock(comp: Competitor) -> Optional[StockSnapshot]:
    return fetch_stock_by_ticker(comp.ticker)


@dataclass
class CompetitorData:
    comp: Competitor
    news: list[NewsItem]
    stock: Optional[StockSnapshot]


def collect_all(cfg: Config) -> list[CompetitorData]:
    results: list[CompetitorData] = []
    for comp in cfg.competitors:
        print(f"  - {comp.name} 수집 중...", file=sys.stderr)
        results.append(CompetitorData(comp=comp, news=fetch_news(comp, cfg), stock=fetch_stock(comp)))
    return results


SUMMARY_SYSTEM_PROMPT = """\
당신은 기업 IR팀의 시장·경쟁사 분석 애널리스트입니다.
수집된 뉴스 제목을 바탕으로, IR 담당자가 1분 안에 읽을 수 있는 간결한 동향 브리핑을 작성하세요.

규칙:
- 추측하지 말고 제공된 정보에 근거해서만 작성합니다. 정보가 부족하면 "특이사항 없음"으로 표기합니다.
- 과장된 전망이나 투자 권유성 표현은 쓰지 않습니다.
- 단순 주가/지수 등락 수치만 다루는 항목은 highlights에 넣지 마세요. 수치는 별도 화면에 이미 표시됩니다.
- 출력은 아래 JSON 형식만 반환합니다. 다른 텍스트나 마크다운 코드펜스를 넣지 마세요.

{
  "tldr": ["핵심 한 줄 요약 1", "핵심 한 줄 요약 2", "핵심 한 줄 요약 3"],
  "highlights": [
    {"competitor": "주제", "category": "실적|공시|뉴스|시장", "headline": "한 줄 제목", "detail": "2~3문장 상세"}
  ]
}
"""


def build_summary_user_prompt(title: str, blocks: list[tuple[str, list[NewsItem]]]) -> str:
    lines = [f"분석 대상: {title}", "수집된 뉴스:\n"]
    for name, news in blocks:
        lines.append(f"### {name}")
        if news:
            for n in news:
                lines.append(f"  · {n.title}")
        else:
            lines.append("  (수집된 뉴스 없음)")
        lines.append("")
    return "\n".join(lines)


def _summarize(title: str, blocks: list[tuple[str, list[NewsItem]]]) -> dict:
    import json
    api_key = os.getenv("ANTHROPIC_API_KEY")
    fallback = _fallback_summary(blocks)
    if not api_key:
        print(f"  ! ANTHROPIC_API_KEY 없음 — {title} 폴백 요약 사용", file=sys.stderr)
        return fallback
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_summary_user_prompt(title, blocks)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  ! {title} LLM 요약 실패 ({e}) — 폴백 사용", file=sys.stderr)
        return fallback


def _fallback_summary(blocks: list[tuple[str, list[NewsItem]]]) -> dict:
    highlights = []
    for name, news in blocks:
        for n in news[:3]:
            highlights.append({
                "competitor": name,
                "category": "뉴스",
                "headline": n.title,
                "detail": f"출처: {n.source or '미상'}.",
            })
    tldr = [h["headline"] for h in highlights[:3]] or ["특이사항 없음"]
    return {"tldr": tldr, "highlights": highlights}


def summarize_competitors(cfg: Config, data: list[CompetitorData]) -> dict:
    blocks = [(d.comp.name, d.news) for d in data]
    return _summarize("건설기계 경쟁사", blocks)


def summarize_kospi(cfg: Config, kospi_news: list[NewsItem]) -> dict:
    return _summarize("코스피 시장", [("코스피", kospi_news)])


CATEGORY_COLORS = {
    "실적": ("#FCEBEB", "#A32D2D"),
    "공시": ("#E6F1FB", "#185FA5"),
    "리포트": ("#FAEEDA", "#854F0B"),
    "뉴스": ("#F1EFE8", "#5F5E5A"),
    "시장": ("#EDE9FB", "#4A3F9E"),
}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_highlights(summary: dict) -> str:
    out = ""
    for h in summary.get("highlights", []):
        cat = h.get("category", "뉴스")
        bg, fg = CATEGORY_COLORS.get(cat, CATEGORY_COLORS["뉴스"])
        out += f"""
        <div style="margin-bottom:14px;">
          <div style="margin-bottom:4px;">
            <span style="font-size:11px;background:{bg};color:{fg};padding:2px 8px;border-radius:8px;">{_esc(cat)}</span>
            <span style="font-size:14px;font-weight:500;margin-left:6px;">{_esc(h.get('headline',''))}</span>
          </div>
          <p style="font-size:13px;color:#5F5E5A;line-height:1.6;margin:0;">{_esc(h.get('detail',''))}</p>
        </div>"""
    return out or '<p style="font-size:13px;color:#999;">특이사항 없음</p>'


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


def render_html(cfg: Config, data: list[CompetitorData],
                comp_summary: dict, kospi_summary: dict,
                kospi_snap: Optional[StockSnapshot], kospi_news: list[NewsItem]) -> str:
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)
    total_news = sum(len(d.news) for d in data) + len(kospi_news)

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

    # 건설기계 기업 카드들 — 지정 순서로 정렬 (국내 우선)
    # 코스피 다음: HD건설기계 → 두산밥캣 → 진성티이씨 → 나머지(해외)
    priority = {"HD건설기계": 0, "두산밥캣": 1, "진성티이씨": 2}
    ordered = sorted(data, key=lambda d: priority.get(d.comp.name, 99))

    stock_cards = ""
    for d in ordered:
        if not (d.stock and d.stock.price is not None):
            continue
        chg = d.stock.change_pct
        color = "#C0392B" if (chg or 0) >= 0 else "#1B6CC4"
        arrow = "▲" if (chg or 0) >= 0 else "▼"
        chg_txt = f"{arrow} {abs(chg):.2f}%" if chg is not None else "—"
        mc_krw = market_cap_in_krw(d.stock)
        mc_txt = format_krw_jo(mc_krw)
        mc_line = f'<p style="font-size:11px;color:#888;margin:4px 0 0;">시총 {mc_txt} 원</p>' if mc_txt else ''
        stock_cards += f"""
        <div style="background:#F7F6F2;border-radius:8px;padding:12px;">
          <p style="font-size:12px;color:#5F5E5A;margin:0;">{_esc(d.comp.name)}</p>
          <p style="font-size:18px;font-weight:500;margin:2px 0;">{d.stock.price:,} <span style="font-size:12px;color:#888;">{_esc(d.stock.currency)}</span></p>
          <p style="font-size:12px;color:{color};margin:0;">{chg_txt}</p>
          {mc_line}
        </div>"""

    comp_blocks = [(d.comp.name, d.news) for d in data]

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="600">
<title>{_esc(cfg.report_title)}</title>
<style>
  .tab-btn {{ flex:1; padding:10px; border:none; background:#EFEDE7; cursor:pointer; font-size:14px; font-weight:500; color:#666; border-radius:8px 8px 0 0; }}
  .tab-btn.active {{ background:#fff; color:#1a1a1a; }}
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
      <p style="font-size:13px;color:#999;margin:8px 0 0;">건설기계 {len(cfg.competitors)}개사 + 코스피 · 지난 {cfg.news_lookback_hours}시간 · 뉴스 {total_news}건 수집</p>
    </div>

    <!-- 코스피 핵심 -->
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <p style="font-size:13px;font-weight:600;color:#4A3F9E;margin:0 0 10px;">코스피 핵심 (TL;DR)</p>
      <ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.7;">{''.join(f'<li>{_esc(t)}</li>' for t in kospi_summary.get('tldr', []))}</ul>
    </div>

    <!-- 주가 스냅샷 -->
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <p style="font-size:13px;font-weight:600;color:#666;margin:0 0 12px;">주가 스냅샷</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;">
        {kospi_card}
        {stock_cards}
      </div>
    </div>

    <!-- 주요 이슈 상세: 탭 -->
    <div style="padding:20px 24px;">
      <div style="display:flex;gap:4px;margin-bottom:16px;">
        <button class="tab-btn active" onclick="showTab('tab-comp')">건설기계 주요이슈</button>
        <button class="tab-btn" onclick="showTab('tab-kospi')">코스피 주요이슈</button>
      </div>

      <div id="tab-comp" class="tab-panel active">
        {_render_highlights(comp_summary)}
        <p style="font-size:13px;font-weight:600;color:#666;margin:18px 0 4px;border-top:1px solid #eee;padding-top:14px;">출처 링크</p>
        {_render_source_links(comp_blocks)}
      </div>

      <div id="tab-kospi" class="tab-panel">
        {_render_highlights(kospi_summary)}
        <p style="font-size:13px;font-weight:600;color:#666;margin:18px 0 4px;border-top:1px solid #eee;padding-top:14px;">출처 링크</p>
        {_render_source_links([("코스피", kospi_news)])}
      </div>
    </div>
  </div>
  <p style="font-size:11px;color:#999;text-align:center;margin:12px 0 0;">자동 생성 리포트 · 요약은 AI가 생성하므로 원문 확인 권장 · 2시간마다 자동 갱신</p>
</div>
<script>
function showTab(id) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body></html>"""


def send_email(cfg: Config, html: str) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    if not (cfg.smtp_host and cfg.mail_from and cfg.mail_to):
        raise RuntimeError("SMTP 설정이 비어 있습니다.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{cfg.report_title} — {dt.datetime.now():%Y-%m-%d}"
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
    parser = argparse.ArgumentParser(description="경쟁사 모니터링 MVP")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="report.html")
    args = parser.parse_args()

    cfg = load_config(args.config)
    print(f"[1/4] 설정 로드 완료 — 건설기계 {len(cfg.competitors)}개사 + 코스피", file=sys.stderr)

    print("[2/4] 데이터 수집 중...", file=sys.stderr)
    data = collect_all(cfg)
    print("  - 코스피 지수·뉴스 수집 중...", file=sys.stderr)
    kospi_snap = fetch_stock_by_ticker(cfg.kospi_ticker)
    kospi_news = fetch_kospi_news(cfg)

    if args.dry_run:
        print("\n=== DRY RUN ===")
        if kospi_snap:
            print(f"[코스피] {kospi_snap.price} ({kospi_snap.change_pct})")
        for n in kospi_news[:5]:
            print(f"  · {n.title}")
        for d in data:
            print(f"\n[{d.comp.name}] 주가={d.stock.price if d.stock else None}")
            for n in d.news:
                print(f"  · {n.title}")
        return

    print("[3/4] LLM 요약 생성 중...", file=sys.stderr)
    comp_summary = summarize_competitors(cfg, data)
    kospi_summary = summarize_kospi(cfg, kospi_news)

    print("[4/4] 리포트 렌더링...", file=sys.stderr)
    html = render_html(cfg, data, comp_summary, kospi_summary, kospi_snap, kospi_news)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  저장 완료 → {args.out}", file=sys.stderr)

    if args.send:
        send_email(cfg, html)


if __name__ == "__main__":
    main()
