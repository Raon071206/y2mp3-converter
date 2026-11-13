# YouTube / SoundCloud 미디어 추출기

YouTube 및 SoundCloud 링크에서 오디오(`MP3`, `M4A`, `OGG`, `WAV`, `FLAC`)와 썸네일(`.jpg`)을 추출하고, 로컬 오디오 파일을 밀리초(`00:00:000`) 단위로 자를 수 있는 `customtkinter` 기반 데스크톱 앱입니다.

## 주요 기능
- **오디오 추출**: YouTube / SoundCloud 단일·일괄(줄바꿈)·재생목록 변환, `128 / 192 / 256 / 320 kbps` 및 무손실(`WAV`, `FLAC`) 지원, 구간 자르기, 메타데이터·앨범 아트 선택 내장
- **썸네일 추출**: 화질별(`maxresdefault`, `sddefault`, `hqdefault`, `mqdefault` 및 자동 최고화질) 추출, `제목(가로x세로).jpg` 자동 저장
- **오디오 자르기**: 로컬 오디오 파일 길이 자동 감지, `00:00:000` 밀리초 단위 무손실 구간 자르기 및 최대 길이 초과 방지
- **편의 기능**: 라이트/다크 모드 무깜빡임 전환, 설정(`config.json`) 자동 저장, 중복 파일명 자동 번호 부여

## 실행 방법 (소스코드)
1. Python 3.12+ 가상환경 생성 및 패키지 설치:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. `bin/` 폴더에 `ffmpeg.exe`, `ffprobe.exe` 배치 (또는 시스템 `PATH`에 등록)
3. 실행:
   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

## `.exe` 빌드 (PyInstaller)
```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconsole --onefile --icon=assets/app_icon.ico --add-data "assets;assets" --add-data "bin;bin" --collect-all customtkinter --name "MediaExtractor" main.py
```

> 개인 학습·소장 용도로만 사용하며, 저작권이 있는 콘텐츠의 무단 배포 및 상업적 이용을 금합니다.
