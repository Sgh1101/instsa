# 인스타 다운로더

인스타그램 **게시물(사진·영상·여러 장 캐러셀)** 과 **릴스** 주소를 붙여 넣으면 미리보기를 보고 내려받을 수 있는 로컬 웹 앱입니다.

- 항목별 다운로드, 여러 개짜리 게시물은 **전체 zip** 다운로드
- 영상은 [yt-dlp](https://github.com/yt-dlp/yt-dlp), 사진은 가장 큰 해상도 원본을 직접 내려받음
- 서버는 기본적으로 내 컴퓨터(`127.0.0.1`)에서만 열림

## 실행

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                        # http://127.0.0.1:5000
```

`HOST`, `PORT` 환경 변수로 주소를 바꿀 수 있습니다. **쿠키(로그인 정보)를 쓰므로 `HOST=0.0.0.0` 등으로 외부에 공개하지 마세요.**

## 지원 범위

| 주소 | 지원 |
|---|---|
| `/p/…` 게시물 (사진·영상·캐러셀) | O |
| `/reel/…`, `/reels/…` 릴스 | O |
| `/tv/…` IGTV | O |
| 스토리, 프로필 전체, 하이라이트 | X (주소 단계에서 안내 후 거부) |

## 쿠키 설정 (로그인이 필요한 경우)

공개 게시물도 인스타그램이 요청을 제한하면 `로그인이 필요한 게시물…` 오류가 날 수 있고, 비공개 계정 게시물(내가 팔로우 중인 경우)은 항상 로그인이 필요합니다. 아래 중 하나를 설정하고 서버를 다시 시작하세요.

```bash
# 방법 1) 브라우저에 로그인된 쿠키를 직접 사용
IG_COOKIES_FROM_BROWSER=chrome python app.py        # firefox, edge, safari 등도 가능

# 방법 2) cookies.txt (Netscape 형식, 브라우저 확장 프로그램으로 내보내기)
IG_COOKIES_FILE=/path/to/cookies.txt python app.py
```

- 쿠키는 **계정 로그인 권한과 같습니다.** 저장소에 커밋하지 마세요 (`cookies*.txt`는 `.gitignore`에 포함).
- 자동화된 접근은 인스타그램이 계정을 일시 제한할 수 있습니다. 가능하면 **본인 부계정**을 쓰고 짧은 시간에 많은 요청을 보내지 마세요.

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest
```

테스트는 네트워크 없이 동작합니다. 가짜 Instagram 추출기와 로컬 HTTP 서버로 yt-dlp의 실제 추출·다운로드 경로(사진 전용 게시물, 영상, 혼합 캐러셀 포함)를 통과시킵니다.

## 구조

```
app.py           Flask 서버: /api/info, /api/thumb, /api/download
igdl.py          추출·다운로드 로직 (Flask 무관)
static/index.html  화면
tests/           pytest
```

## 알려진 한계

- **인스타그램이 내부 구조를 바꾸면 깨질 수 있습니다.** 대부분 yt-dlp 업데이트로 해결됩니다: `pip install -U yt-dlp`
- 사진 전용 게시물은 yt-dlp가 직접 지원하지 않아, 추출된 이미지 후보 중 가장 큰 것을 받습니다. 이 방식도 인스타그램 응답 형식에 의존합니다.
- 다운로드는 서버가 임시 폴더에 받은 뒤 브라우저로 전송합니다(전송이 끝나거나 연결이 끊기면 삭제). 매우 큰 영상은 시간이 걸립니다.
- 한 번에 한 게시물만 처리합니다. 대량 수집용이 아닙니다.

## 이용 시 주의

본인이 올렸거나 저작권자의 허락을 받은 콘텐츠만 내려받으세요. 타인의 콘텐츠를 허락 없이 저장·재게시하면 저작권 침해가 될 수 있고, 자동화된 수집은 인스타그램 이용약관에 어긋날 수 있습니다. 사용에 따른 책임은 사용자에게 있습니다.
