"""
경쟁사 모니터링 자동화 MVP
===========================

매 영업일 아침, 모니터링 대상 경쟁사의 뉴스와 주가를 수집해
Claude로 요약한 뒤 HTML 리포트를 생성하고 메일로 발송합니다.

수집 소스 (MVP 범위):
  - 뉴스: Google News RSS (키워드 기반, 무료, 인증 불필요)
  - 주가: yfinance (무료, 글로벌 티커 지원)

확장 예정 (2단계 이후):
  - 공시: DART OpenAPI (국내), SEC EDGAR (해외)
  - 컨센서스: 사내 구독 데이터 벤더 연동

사용법:
  1. config.yaml 에서 모니터링 대상과 발송 설정을 수정
  2. 환경변수 설정: ANTHROPIC_API_KEY, (메일 발송 시) SMTP_* 변수
  3. python competitor_monitor.py            # 리포트 생성 + 저장
     python competitor_monitor.py --send     # 생성 후 메일 발송
     python competitor_monitor.py --dry-run  # LLM/메일 없이 수집만 테스트
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
except ImportError:  # 주가 수집은 선택 기능 — 미설치 시 자동 생략
    yf = None


# ---------------------------------------------------------------------------
# 설정 로드
# ---------------------------------------------------------------------------

@dataclass
class Competitor:
    name: str           # 표시 이름 (예: "A사")
    keywords: list[str] # 뉴스 검색 키워드
    ticker: str = ""    # 주가 티커 (예: "005930.KS", "NVDA"). 없으면 주가 생략
    region: str = "KR"  # 표시용 지역 태그
    lang: str = ""      # 이 회사 뉴스 언어 (예: "ko","en"). 비우면 전체 기본값 사용
    country: str = ""   # 이 회사 뉴스 국가 (예: "KR","US"). 비우면 전체 기본값 사용


@dataclass
class Config:
    company_name: str
    competitors: list[Competitor]
    news_lookback_hours: int = 24
    max_news_per_competitor: int = 5
    news_language: str = "ko"      # Google News hl 파라미터
    news_country: str = "KR"       # Google News gl 파라미터
    report_title: str = "경쟁사 동향 데일리 브리핑"
    # 메일 발송 설정 (환경변수로 채워짐)
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

    cfg = Config(
        company_name=raw["company_name"],
        competitors=competitors,
        news_lookback_hours=raw.get("news_lookback_hours", 24),
        max_news_per_competitor=raw.get("max_news_per_competitor", 5),
        news_language=raw.get("news_language", "ko"),
        news_country=raw.get("news_country", "KR"),
        report_title=raw.get("report_title", "경쟁사 동향 데일리 브리핑"),
        mail_to=raw.get("mail_to", []),
    )
    return cfg


# ---------------------------------------------------------------------------
# 데이터 수집
# ---------------------------------------------------------------------------

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
    error: str = ""


def fetch_news(comp: Competitor, cfg: Config) -> list[NewsItem]:
    """Google News RSS에서 키워드 기반 뉴스 수집."""
    # 회사별 언어/국가가 지정돼 있으면 그것을 쓰고, 없으면 전체 기본값
    lang = comp.lang or cfg.news_language
    country = comp.country or cfg.news_country
    query = " OR ".join(f'"{k}"' for k in comp.keywords)
    params = {
        "q": query,
        "hl": lang,
        "gl": country,
        "ceid": f"{country}:{lang}",
    }
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)

    feed = feedparser.parse(url)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cfg.news_lookback_hours)

    items: list[NewsItem] = []
    for entry in feed.entries:
        published = None
        if getattr(entry, "published_parsed", None):
            published = dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc)
            if published < cutoff:
                continue  # 수집 기간 밖
        source = ""
        if getattr(entry, "source", None):
            source = entry.source.get("title", "")
        items.append(
            NewsItem(
                title=entry.title,
                link=entry.link,
                published=published,
                source=source,
            )
        )
        if len(items) >= cfg.max_news_per_competitor:
            break
    return items


def fetch_stock(comp: Competitor) -> Optional[StockSnapshot]:
    """yfinance로 최근 종가와 전일 대비 등락률 수집."""
    if not comp.ticker:
        return None
    snap = StockSnapshot(ticker=comp.ticker)
    if yf is None:
        snap.error = "yfinance 미설치"
        return snap
    try:
        # 1개월치를 받아 연휴·휴장이 끼어도 최근 두 거래일을 확보
        hist = yf.Ticker(comp.ticker).history(period="1mo")
        if hist is not None and not hist.empty:
            # yfinance가 가끔 끼워넣는 NaN 종가 행을 제거 (NaN% 원인)
            hist = hist.dropna(subset=["Close"])
        if hist is None or hist.empty:
            snap.error = "가격 데이터 없음"
            return snap

        last = float(hist["Close"].iloc[-1])
        snap.price = round(last, 2)

        # 최근 두 거래일이 모두 있을 때만 등락률 계산, 아니면 비워둠(— 표시)
        if len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            if prev:
                snap.change_pct = round((last - prev) / prev * 100, 2)

        try:
            snap.currency = yf.Ticker(comp.ticker).fast_info.get("currency", "") or ""
        except Exception:
            snap.currency = ""
    except Exception as e:  # noqa: BLE001 - 수집 실패는 리포트에 표기만
        snap.error = str(e)[:120]
    return snap


@dataclass
class CompetitorData:
    comp: Competitor
    news: list[NewsItem]
    stock: Optional[StockSnapshot]


def collect_all(cfg: Config) -> list[CompetitorData]:
    results: list[CompetitorData] = []
    for comp in cfg.competitors:
        print(f"  - {comp.name} 수집 중...", file=sys.stderr)
        news = fetch_news(comp, cfg)
        stock = fetch_stock(comp)
        results.append(CompetitorData(comp=comp, news=news, stock=stock))
    return results


# ---------------------------------------------------------------------------
# LLM 요약 (Claude)
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """\
당신은 기업 IR팀의 경쟁사 분석 애널리스트입니다.
수집된 경쟁사 뉴스 제목과 주가 정보를 바탕으로, 자사 IR 담당자가
아침에 1분 안에 읽을 수 있는 간결한 동향 브리핑을 작성하세요.

규칙:
- 추측하지 말고 제공된 정보에 근거해서만 작성합니다. 정보가 부족하면 "특이사항 없음"으로 표기합니다.
- 과장된 전망이나 투자 권유성 표현은 쓰지 않습니다.
- 단순 주가 등락(예: "주가 +1.3%")만 다루는 항목은 highlights에 넣지 마세요. 주가는 별도 화면에 이미 표시됩니다. 실적·공시·사업 등 실제 뉴스성 이슈만 담습니다.
- 출력은 아래 JSON 형식만 반환합니다. 다른 텍스트나 마크다운 코드펜스를 넣지 마세요.

{
  "tldr": ["핵심 한 줄 요약 1", "핵심 한 줄 요약 2", "핵심 한 줄 요약 3"],
  "highlights": [
    {"competitor": "회사명", "category": "실적|공시|뉴스", "headline": "한 줄 제목", "detail": "2~3문장 상세"}
  ]
}
"""


def build_summary_user_prompt(cfg: Config, data: list[CompetitorData]) -> str:
    lines = [f"자사: {cfg.company_name}", "수집된 경쟁사 정보:\n"]
    for d in data:
        lines.append(f"### {d.comp.name} ({d.comp.region})")
        if d.stock and d.stock.price is not None:
            chg = f"{d.stock.change_pct:+.2f}%" if d.stock.change_pct is not None else "N/A"
            lines.append(f"- 주가: {d.stock.price} {d.stock.currency} (전일 대비 {chg})")
        if d.news:
            lines.append("- 뉴스 제목:")
            for n in d.news:
                lines.append(f"  · {n.title}")
        else:
            lines.append("- 뉴스: 수집된 항목 없음")
        lines.append("")
    return "\n".join(lines)


def summarize_with_claude(cfg: Config, data: list[CompetitorData]) -> dict:
    """Claude API 호출. 실패 시 폴백 요약 반환."""
    import json

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ! ANTHROPIC_API_KEY 없음 — 폴백 요약 사용", file=sys.stderr)
        return _fallback_summary(data)

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_summary_user_prompt(cfg, data)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"  ! LLM 요약 실패 ({e}) — 폴백 사용", file=sys.stderr)
        return _fallback_summary(data)


def _fallback_summary(data: list[CompetitorData]) -> dict:
    """LLM 미사용 시 수집 데이터만으로 구성하는 기본 요약."""
    highlights = []
    for d in data:
        # 단순 주가 등락은 위쪽 '주가 스냅샷'에 이미 표시되므로
        # 주요 이슈 상세에는 넣지 않고 실제 뉴스성 이슈만 담는다.
        for n in d.news[:3]:
            highlights.append({
                "competitor": d.comp.name,
                "category": "뉴스",
                "headline": n.title,
                "detail": f"출처: {n.source or '미상'}.",
            })
    tldr = [h["headline"] for h in highlights[:3]] or ["지난 기간 특이사항 없음"]
    return {"tldr": tldr, "highlights": highlights}


# ---------------------------------------------------------------------------
# 리포트 렌더링 (HTML)
# ---------------------------------------------------------------------------

CATEGORY_COLORS = {
    "실적": ("#FCEBEB", "#A32D2D"),
    "공시": ("#E6F1FB", "#185FA5"),
    "리포트": ("#FAEEDA", "#854F0B"),
    "뉴스": ("#F1EFE8", "#5F5E5A"),
    "주가": ("#E1F5EE", "#0F6E56"),
}


def render_html(cfg: Config, data: list[CompetitorData], summary: dict) -> str:
    now = dt.datetime.now()
    total_news = sum(len(d.news) for d in data)

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    tldr_items = "".join(f"<li>{esc(t)}</li>" for t in summary.get("tldr", []))

    stock_cards = ""
    for d in data:
        if not (d.stock and d.stock.price is not None):
            continue
        chg = d.stock.change_pct
        color = "#0F6E56" if (chg or 0) >= 0 else "#A32D2D"
        arrow = "▲" if (chg or 0) >= 0 else "▼"
        chg_txt = f"{arrow} {abs(chg):.2f}%" if chg is not None else "—"
        stock_cards += f"""
        <div style="background:#F7F6F2;border-radius:8px;padding:12px;">
          <p style="font-size:12px;color:#5F5E5A;margin:0;">{esc(d.comp.name)}</p>
          <p style="font-size:18px;font-weight:500;margin:2px 0;">{d.stock.price:,} <span style="font-size:12px;color:#888;">{esc(d.stock.currency)}</span></p>
          <p style="font-size:12px;color:{color};margin:0;">{chg_txt}</p>
        </div>"""

    highlight_blocks = ""
    for h in summary.get("highlights", []):
        cat = h.get("category", "뉴스")
        bg, fg = CATEGORY_COLORS.get(cat, CATEGORY_COLORS["뉴스"])
        highlight_blocks += f"""
        <div style="margin-bottom:14px;">
          <div style="margin-bottom:4px;">
            <span style="font-size:11px;background:{bg};color:{fg};padding:2px 8px;border-radius:8px;">{esc(cat)}</span>
            <span style="font-size:14px;font-weight:500;margin-left:6px;">{esc(h.get('headline',''))}</span>
          </div>
          <p style="font-size:13px;color:#5F5E5A;line-height:1.6;margin:0;">{esc(h.get('detail',''))}</p>
        </div>"""

    source_links = ""

    def render_link(n) -> str:
        # 발행일시(UTC)를 한국시간 날짜로 변환해 표시, 없으면 날짜 생략
        date_txt = ""
        if n.published is not None:
            kst = n.published + dt.timedelta(hours=9)
            date_txt = f'<span style="color:#999;"> · {kst:%Y-%m-%d}</span>'
        return (
            f'<li><a href="{esc(n.link)}" style="color:#185FA5;text-decoration:none;">{esc(n.title)}</a>'
            f' <span style="color:#999;">— {esc(n.source)}</span>{date_txt}</li>'
        )

    for d in data:
        if not d.news:
            continue
        # 발행일 최신순 정렬 (날짜 없는 항목은 뒤로)
        news_sorted = sorted(
            d.news,
            key=lambda n: n.published or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            reverse=True,
        )
        links = "".join(render_link(n) for n in news_sorted)
        source_links += f'<p style="font-size:13px;font-weight:500;margin:10px 0 4px;">{esc(d.comp.name)}</p><ul style="margin:0 0 8px;padding-left:18px;font-size:12px;line-height:1.6;">{links}</ul>'

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>{esc(cfg.report_title)}</title></head>
<body style="margin:0;background:#EFEDE7;font-family:-apple-system,'Segoe UI',sans-serif;color:#1a1a1a;">
<div style="max-width:680px;margin:0 auto;padding:20px;">
  <div style="background:#fff;border-radius:12px;border:1px solid #e3e1d9;overflow:hidden;">
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:center;">
        <span style="font-size:18px;font-weight:600;">📡 {esc(cfg.report_title)}</span>
        <span style="font-size:13px;color:#777;">{now:%Y년 %m월 %d일} {now:%H:%M}</span>
      </div>
      <p style="font-size:13px;color:#999;margin:8px 0 0;">모니터링 대상 {len(cfg.competitors)}개사 · 지난 {cfg.news_lookback_hours}시간 · 뉴스 {total_news}건 수집</p>
    </div>
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <p style="font-size:13px;font-weight:600;color:#666;margin:0 0 10px;">오늘의 핵심 (TL;DR)</p>
      <ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.7;">{tldr_items}</ul>
    </div>
    {'<div style="padding:20px 24px;border-bottom:1px solid #eee;"><p style="font-size:13px;font-weight:600;color:#666;margin:0 0 12px;">주가 스냅샷</p><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;">' + stock_cards + '</div></div>' if stock_cards else ''}
    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <p style="font-size:13px;font-weight:600;color:#666;margin:0 0 12px;">주요 이슈 상세</p>
      {highlight_blocks or '<p style="font-size:13px;color:#999;">특이사항 없음</p>'}
    </div>
    <div style="padding:20px 24px;">
      <p style="font-size:13px;font-weight:600;color:#666;margin:0 0 4px;">출처 링크</p>
      {source_links or '<p style="font-size:12px;color:#999;">수집된 뉴스 없음</p>'}
    </div>
  </div>
  <p style="font-size:11px;color:#999;text-align:center;margin:12px 0 0;">자동 생성 리포트 · 요약은 AI가 생성하므로 원문 확인 권장 · 매 영업일 발송</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# 메일 발송
# ---------------------------------------------------------------------------

def send_email(cfg: Config, html: str) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not (cfg.smtp_host and cfg.mail_from and cfg.mail_to):
        raise RuntimeError("SMTP 설정(SMTP_HOST/MAIL_FROM/mail_to)이 비어 있습니다.")

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


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="경쟁사 모니터링 MVP")
    parser.add_argument("--config", default="config.yaml", help="설정 파일 경로")
    parser.add_argument("--send", action="store_true", help="생성 후 메일 발송")
    parser.add_argument("--dry-run", action="store_true", help="LLM/메일 없이 수집만 테스트")
    parser.add_argument("--out", default="report.html", help="HTML 저장 경로")
    args = parser.parse_args()

    cfg = load_config(args.config)
    print(f"[1/4] 설정 로드 완료 — 대상 {len(cfg.competitors)}개사", file=sys.stderr)

    print("[2/4] 데이터 수집 중...", file=sys.stderr)
    data = collect_all(cfg)

    if args.dry_run:
        print("\n=== DRY RUN 수집 결과 ===")
        for d in data:
            print(f"\n[{d.comp.name}]")
            if d.stock:
                print(f"  주가: {d.stock.price} {d.stock.currency} "
                      f"({d.stock.change_pct:+.2f}%)" if d.stock.price else f"  주가: {d.stock.error}")
            for n in d.news:
                print(f"  · {n.title}")
        return

    print("[3/4] LLM 요약 생성 중...", file=sys.stderr)
    summary = summarize_with_claude(cfg, data)

    print("[4/4] 리포트 렌더링...", file=sys.stderr)
    html = render_html(cfg, data, summary)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  저장 완료 → {args.out}", file=sys.stderr)

    if args.send:
        send_email(cfg, html)


if __name__ == "__main__":
    main()
