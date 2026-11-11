# Discord Coin BOT

원화를 입금받아 코인으로 보내 주는 디스코드 봇입니다.
유저는 디스코드 패널에서 원화를 충전하고, 그 잔액으로 코인을 골라 원하는 지갑 주소로 보냅니다.
코인은 운영자의 바이낸스 계정에서 출금되며, 재고가 모자라면 그 자리에서 USDT로 사서 보냅니다.

## 동작 방식

1. 관리자가 유저에게 입금자명을 지정합니다. 이 이름으로 들어온 입금만 그 유저의 것으로 인정합니다.
2. 유저가 패널에서 금액을 넣고 충전을 신청하면 계좌와 입금자명을 안내합니다.
3. 은행 입금 문자가 웹훅으로 들어오면, 입금자명과 금액이 열려 있는 신청과 같을 때 잔액에 충전합니다.
   맞지 않는 입금은 관리자 로그 채널에 올라가고 관리자가 직접 처리합니다.
4. 유저가 코인·네트워크·주소·금액을 고르면 견적을 보여 주고, 확인하면 바이낸스에서 출금합니다.
5. 출금 결과는 바이낸스 기록과 주기적으로 대조합니다. 접수되지 않은 출금은 자동으로 환불합니다.

가격은 `바이낸스 시세 × 환율 × (1 + 시장 김프 + 추가 김프)`이고, 여기에 수수료가 붙습니다.
USDT가 아닌 코인은 사는 데 드는 값(호가, 거래 수수료)이 견적에 들어갑니다.

## 주요 기능

- 원화 충전: 입금 문자 자동 매칭, 요청 마감, 미확인 입금 처리
- 코인 송금: 견적과 재확인, 주소·메모 형식 검사, TXID 조회, 주소록
- 한도: 1회, 24시간, 신규 유저, 서비스 전체
- 운영: 로그 채널과 알림, 수동 충전·차감, 이용 정지, 서비스 중지, 이익 조회, 일일 요약
- 보호 장치: 이중 충전 방지, 입금 취소·지급정지 문자 대응, 출금 결과 불명 시 추적과 환불

## 준비물

- Python 3.10 이상
- 디스코드 봇 토큰
- 바이낸스 API 키 (현물 거래와 출금 권한이 필요합니다. IP 제한을 꼭 걸어 두세요)
- 은행 입금 문자를 웹훅으로 넘겨 줄 수단 (예: iPhone 단축어 자동화). 지금은 KB국민은행 문자 형식만 읽습니다.

## 설치와 실행

```bash
git clone https://github.com/Utia-love08/Discord_Coin_BOT.git
cd Discord_Coin_BOT
python -m venv venv
venv/bin/pip install -r requirements.lock

cp .env.example .env        # 값을 채웁니다
venv/bin/python deploy_commands.py   # 슬래시 명령 등록 (처음 한 번, 명령이 바뀌었을 때)
venv/bin/python main.py
```

봇이 켜지면 관리자가 원하는 채널에서 `/패널`을 실행해 메뉴를 올리고,
`/관리자 로그채널설정`으로 운영 기록을 받을 채널을 정합니다.

## 설정 (.env)

비워 두면 기본값을 씁니다. 금액은 원 단위이고, 한도는 0이면 끕니다.

| 이름 | 설명 | 기본값 |
| --- | --- | --- |
| `DISCORD_TOKEN` | 봇 토큰 | 필수 |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | 바이낸스 API 키 | 필수 |
| `ADMIN_USERS` | 관리자 디스코드 ID (쉼표로 구분) | 필수 |
| `ALLOWED_USERS` | 입금자명 없이도 봇을 쓸 수 있는 유저 ID | 없음 |
| `BANK_NAME`, `BANK_ACCOUNT`, `BANK_HOLDER` | 유저에게 안내할 입금 계좌 | 없음 |
| `WEBHOOK_SECRET` | 입금 웹훅 키 (32자 이상의 무작위 값) | 필수 |
| `WEBHOOK_SIGNED` | `true`면 서명 헤더가 있는 요청만 받음 | `false` |
| `API_HOST`, `API_PORT` | 웹훅 서버 주소와 포트 | `127.0.0.1`, `8080` |
| `API_SSL_CERT`, `API_SSL_KEY` | 웹훅을 직접 HTTPS로 받을 때의 인증서 | 없음 |
| `FEE_RATE` | 수수료율 (0.02 = 2%). `/수수료`로 바꾸면 그 값이 우선 | `0.02` |
| `MIN_KIMCHI_PREMIUM_PCT` | 시장 김프에 더하는 값(%). `/김프`로 바꾸면 그 값이 우선 | `0.05` |
| `DEPOSIT_TIMEOUT_MINUTES` | 충전 요청 마감 시간(분) | `5` |
| `MAX_TRANSFER_KRW` | 1회 송금 한도 | `1000000` |
| `DAILY_TRANSFER_LIMIT_KRW` | 유저별 24시간 한도 | `3000000` |
| `NEW_USER_DAILY_LIMIT_KRW`, `NEW_USER_DAYS`, `NEW_USER_MIN_TOTAL_KRW` | 신규 유저의 24시간 한도와 신규로 보는 기간·누적 금액 | `300000`, `7`, `0` |
| `GLOBAL_DAILY_LIMIT_KRW` | 전체 유저의 24시간 송금 합계 상한 | `10000000` |
| `MAX_REJECTED_TRANSFERS`, `REJECTED_WINDOW_MINUTES` | 출금 거절이 반복될 때 잠시 막는 기준 | `3`, `60` |
| `LOW_FUNDS_USDT` | 매수 자금이 이 아래로 내려가면 경고 | `10` |
| `TRANSFER_COINS` | 판매할 코인과 메뉴 순서 | `USDT,BTC,ETH,XRP,LTC,SOL,TRX,BNB` |
| `PANEL_BANNER_URL`, `PANEL_THUMBNAIL_URL` | 패널 이미지 (https) | 없음 |
| `VERIFY_LINK_SECRET` | 본인인증 봇 연동 키. 비우면 연동을 끔 | 없음 |
| `DB_PATH` | SQLite 파일 위치 | `data/bot.db` |

## 입금 웹훅

은행 문자 원문을 그대로 보냅니다. 봇이 계좌, 시각, 입금자명, 금액을 읽고 같은 문자가 두 번 와도 한 번만 처리합니다.

```
POST /api/deposit
Content-Type: application/json

{"text": "<문자 원문>", "key": "<WEBHOOK_SECRET>"}
```

`WEBHOOK_SIGNED=true`면 키를 본문에 넣는 대신 헤더로 서명을 보냅니다.

- `X-Timestamp`: 유닉스 초 또는 ISO 8601 시각
- `X-Signature`: `타임스탬프`, `문자 원문`, `WEBHOOK_SECRET`을 줄바꿈으로 이은 문자열의 SHA-256 (hex)

웹훅은 기본적으로 `127.0.0.1`에서만 받습니다. 밖에서 받으려면 HTTPS 터널이나 리버스 프록시 뒤에 두세요.
프록시가 `http://`로 들어왔다고 알려 준 요청은 키가 맞아도 거절합니다. `GET /api/health`는 상태 확인용입니다.

## 명령어

유저

- `/잔액`: 내 잔액 확인. 충전과 송금은 패널의 버튼으로 합니다.

관리자 (`ADMIN_USERS`에 있는 계정만)

- `/패널`, `/수수료`, `/김프`, `/이익`
- `/관리자 입금자명지정`, `입금자명해제`, `입금자명목록`
- `/관리자 미확인입금목록`, `미확인입금충전`, `미확인입금제외`
- `/관리자 잔액충전`, `잔액차감`, `신규한도`
- `/관리자 이용정지`, `이용정지해제`, `서비스중지`, `서비스재개`
- `/관리자 유저조회`, `송금조회`, `로그조회`, `운영상태`
- `/관리자 로그채널설정`, `구매알림설정`, `시세채널설정`, `입금자역할설정`, `구매자역할설정`, `미등록안내설정`

## 테스트

실제 봇 코드에 가짜 바이낸스·시세·디스코드를 붙여 오프라인으로 돌립니다. 실제 서비스에는 아무것도 보내지 않습니다.

```bash
for name in attacks regress hardening ops_fixes verify_link user_limit buy_cost profit screens; do
  venv/bin/python -I -B tests/pentest/$name.py
done
```

## 운영 참고

- `scripts/backup_db.py`: SQLite 온라인 백업. cron으로 하루 한 번 돌리면 봇이 백업 상태를 일일 요약에 같이 알려 줍니다.
- `scripts/coin-bot.service`, `scripts/deploy.sh`: systemd 유닛과 배포 스크립트 예시입니다. 경로와 계정 이름은 서버에 맞게 바꿔야 합니다.

## 주의

- 실제 돈과 코인을 움직이는 프로그램입니다. 충분히 시험한 뒤에 쓰고, 사용에 따른 책임은 사용하는 사람에게 있습니다.
- 가상자산을 대신 사서 보내 주는 일을 영업으로 하면 나라에 따라 신고나 인가가 필요할 수 있습니다. 관련 법규는 직접 확인하세요.
- 바이낸스 API 키는 IP 제한을 걸고 필요한 권한만 주세요. 봇을 돌리는 서버의 `.env`와 DB는 다른 계정이 읽을 수 없게 두세요.

## 라이선스

[MIT](LICENSE)
