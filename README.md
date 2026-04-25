# Private Board Watch

Cloudflare Pages에서 개인용으로 쓰는 공개 게시판 썸네일 링크 대시보드입니다. 원본 이미지를 저장하지 않고, 공개 게시글의 원문 링크와 이미지 URL만 캐시해서 보여줍니다.

## 구조

- `public/`: Cloudflare Pages 정적 파일
- `functions/api/feed.js`: 웹앱이 읽는 `/api/feed`
- `crawler/crawl_to_kv.py`: 공개 게시판을 읽어 Cloudflare KV에 최신 피드를 저장하는 Python 스크립트
- `crawler/board_sources.json`: 게시판 목록, 키워드, 차단 키워드 설정

## 로컬 웹앱 실행

```bash
npm install
npm.cmd run dev
```

브라우저에서 `http://127.0.0.1:8788` 또는 `http://localhost:8788`로 확인합니다.

## 크롤러 dry-run

먼저 업로드 없이 로컬 JSON 생성만 확인하세요.

```bash
pip install -r crawler/requirements.txt
python crawler/crawl_to_kv.py --dry-run
```

결과는 `crawler/feed.latest.json`에 저장됩니다. 이 파일은 `.gitignore`에 포함되어 GitHub에 올라가지 않습니다.

## Cloudflare KV 업로드

토큰은 채팅이나 GitHub에 올리지 말고 로컬 환경변수로만 설정하세요.

```bash
set CLOUDFLARE_ACCOUNT_ID=your_account_id
set CLOUDFLARE_KV_NAMESPACE_ID=your_namespace_id
set CLOUDFLARE_API_TOKEN=your_token
python crawler/crawl_to_kv.py
```

Pages Function에서 KV를 읽으려면 Cloudflare Pages 프로젝트에 KV namespace binding을 추가합니다.

- Binding name: `FEED_KV`
- KV key: 기본값 `feed:latest`
- 다른 key를 쓰려면 Pages 환경변수 `FEED_KV_KEY` 또는 `KV_KEY`를 설정

## 자동 갱신

GitHub Actions가 30분마다 크롤러를 실행해서 Cloudflare KV의 `feed:latest`를 갱신합니다.

- Workflow: `.github/workflows/refresh-feed.yml`
- Schedule: 30분마다 실행, GitHub cron은 UTC 기준입니다.
- 기존 KV 값을 먼저 내려받은 뒤 새 크롤링 결과와 병합하므로 중복은 제거하고 누적합니다.
- Manual run: GitHub Actions 탭에서 `Refresh feed` 워크플로를 `Run workflow`로 실행

GitHub 저장소의 `Settings > Secrets and variables > Actions`에 아래 repository secrets를 등록해야 합니다.

- `CLOUDFLARE_ACCOUNT_ID`: `30004ae152eccf899701379d0aab7ab6`
- `CLOUDFLARE_KV_NAMESPACE_ID`: `10c1130443f34a948090a49d453ca8cd`
- `CLOUDFLARE_API_TOKEN`: Cloudflare KV write 권한이 있는 API token

## 배포

Cloudflare Pages Git 연동 설정:

- Framework preset: `None`
- Build command: 비워두거나 `npm install`
- Build output directory: `public`

현재 만든 Cloudflare 리소스:

- Pages project: `private-board-watch`
- Pages URL: `https://private-board-watch.pages.dev`
- KV namespace: `private-board-watch-feed`
- KV binding: `FEED_KV`
- KV key: `feed:latest`

```bash
npm.cmd run deploy
```

## 다른 PC로 옮기기

채팅 내용을 전부 복사할 필요는 없습니다. 프로젝트 폴더만 옮기면 됩니다.

가져갈 파일:

- `public/`
- `functions/`
- `crawler/`
- `package.json`
- `wrangler.toml`
- `README.md`
- `.gitignore`
- `.cfignore`

가져가지 않아도 되는 파일:

- `node_modules/`
- `.wrangler/`
- `crawler/feed.latest.json`
- `*.zip`
- `.env`
- `wrangler-dev.*.log`

새 PC에서 다시 준비:

```bash
npm install
npm.cmd run dev
```

Python 크롤러를 실행하려면 새 PC에 Python을 설치한 뒤:

```bash
pip install -r crawler/requirements.txt
python crawler/crawl_to_kv.py --dry-run
```

## 개인 접근 제한

GitHub 저장소를 private로 만들어도 Pages 배포 URL은 기본적으로 공개입니다. 테스트 후 Cloudflare Zero Trust Access에서 본인 이메일만 허용하는 정책을 붙이세요.

## 안전 설정

`crawler/board_sources.json`의 `blocked_keywords`는 기본 차단 키워드입니다. 비동의 촬영물, 미성년 관련 표현, 불법 자료와 관련된 단어는 유지하거나 더 추가하는 것을 권장합니다.
