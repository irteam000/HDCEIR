# 경쟁사 모니터링 자동화 MVP

매 영업일 아침, 경쟁사의 뉴스와 주가를 자동 수집해 Claude로 요약하고
HTML 리포트를 생성·메일 발송하는 도구입니다.

IR팀이 매일 수동으로 하던 경쟁사 동향 파악을 자동화하는 것이 목적입니다.

## 무엇을 하나

1. 설정한 경쟁사별로 최근 24시간 뉴스(Google News RSS)와 주가(yfinance) 수집
2. Claude가 수집 데이터를 IR 관점의 브리핑으로 요약 (TL;DR + 이슈 상세)
3. 출처 링크를 포함한 HTML 리포트 생성
4. (선택) 지정 수신자에게 메일 발송

수집 소스는 모두 무료이며 별도 구독·인증이 필요 없습니다.

## 빠른 시작

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. 설정 수정 — config.yaml 에서 경쟁사/수신자 변경

# 3. 수집만 테스트 (LLM·메일 없이, 잘 수집되는지 확인)
python competitor_monitor.py --dry-run

# 4. 리포트 생성 (report.html 저장)
export ANTHROPIC_API_KEY="sk-ant-..."
python competitor_monitor.py

# 5. 메일까지 발송
export SMTP_HOST="smtp.office365.com"
export SMTP_USER="ir-bot@ourcompany.com"
export SMTP_PASSWORD="..."
export MAIL_FROM="ir-bot@ourcompany.com"
python competitor_monitor.py --send
```

`ANTHROPIC_API_KEY`가 없으면 LLM 요약 대신 수집 데이터를 그대로 보여주는
폴백 모드로 동작합니다(키 발급 전에도 동작 확인 가능).

## 설정 (config.yaml)

경쟁사별로 `name`, `keywords`(뉴스 검색어), `ticker`(주가, 생략 가능), `region`을 지정합니다.
티커 형식: 국내는 `005930.KS`(코스피)·`000660.KS`, 미국은 `NVDA`·`MU`, 대만은 `TSM` 등.

## 매일 자동 실행

`.github/workflows/daily-report.yml`에 GitHub Actions 스케줄 예시가 들어 있습니다.
리포지토리 Secrets에 `ANTHROPIC_API_KEY`, `SMTP_*`, `MAIL_FROM`을 등록하면
매 영업일 오전 8시(KST)에 자동 발송됩니다.

사내 서버에서 돌린다면 cron으로도 가능합니다:

```cron
# 매주 월~금 오전 8시 (KST 기준 서버에서)
0 8 * * 1-5  cd /path/to/app && /usr/bin/python3 competitor_monitor.py --send
```

## 한계와 다음 단계 (중요)

이 MVP는 의도적으로 가볍게 만들었습니다. 실제 사내 배포 전 다음을 고려하세요.

- 요약은 AI가 생성하므로 오류 가능성이 있습니다. 리포트에 항상 출처 링크를
  포함하며, 중요한 의사결정 전에는 원문 확인이 필요합니다.
- Google News RSS는 제목 위주 수집입니다. 본문 전체 분석이 필요하면
  기사 본문 크롤링 또는 뉴스 API(유료) 연동이 필요합니다.
- 공시(DART·SEC)와 증권사 컨센서스는 이 MVP에 포함되지 않았습니다.
  2단계에서 DART OpenAPI(무료)·SEC EDGAR(무료)를 추가하고, 컨센서스는
  사내 구독 데이터 벤더와 연동하는 것을 권장합니다.
- 뉴스 저작권: 제목·링크·요약까지만 다루고 본문 전문은 재배포하지 마세요.

## 파일 구성

```
competitor_monitor.py   메인 스크립트 (수집·요약·렌더·발송)
config.yaml             모니터링 대상·발송 설정
requirements.txt        의존성
.github/workflows/      매일 자동 실행 스케줄 예시
```
