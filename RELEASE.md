# DFOGANG Raid Helper — v1.0beta 배포 가이드

이 문서는 **개발자용 빌드 절차**와 **최종 사용자에게 전달할 안내**를
모두 다룹니다. 배포 형태는 PyInstaller onedir + Cython로 보호된
`.pyd` 모듈을 묶은 ZIP입니다. 단일 EXE(onefile)는 사용하지 않습니다.

---

## 1. 최종 사용자 안내 (배포물에 함께 전달)

별도의 Python 설치, `pip`, `.bat` 실행이 **필요하지 않습니다.**

1. `DFOGANG_RaidHelper_v1.0beta.zip` 압축을 해제합니다.
2. 풀린 `DFOGANG_RaidHelper` 폴더 안의 `DFOGANG_RaidHelper.exe`를 실행합니다.
3. 처음 실행 시 PaddleOCR 모델이 자동으로 다운로드되어 로컬 캐시로
   복사됩니다 (인터넷 필요, 한 번만 수행).
4. 이후에는 오프라인으로도 실행 가능합니다.

ZIP 안에는 다음이 들어 있습니다:

```
DFOGANG_RaidHelper/
├── DFOGANG_RaidHelper.exe
└── _internal/
    ├── (PyInstaller 런타임 파일)
    ├── markers/        # 파티/레이드 헤더 마커 이미지
    ├── templates/      # OCR 템플릿
    ├── resources/      # 아이콘 등 부가 리소스
    ├── *.pyd           # 보호된 어플리케이션 모듈
    ├── logo.png
    ├── favicon.ico
    └── DNFForgedBlade-Bold.ttf
```

`.pyd` 파일은 Cython으로 컴파일된 네이티브 확장입니다. 원본 `src/*.py`는
배포물에 포함되지 않습니다.

---

## 2. 개발자 빌드 절차

### 사전 요구사항

| 항목 | 비고 |
| --- | --- |
| Windows 10/11 x64 | 빌드 환경 = 타겟 환경 |
| Python 3.10 (권장) | 개발 시 사용 중인 버전 |
| Visual Studio Build Tools 2019/2022 | "Desktop development with C++" 워크로드 + Windows 10/11 SDK 필수 |
| `pyinstaller`, `cython`, `setuptools`, `wheel` | 빌드 스크립트가 자동 설치/업그레이드 |

Visual Studio 워크로드가 미설치되면 Cython 단계가 `pyconfig.h fatal error
C1083: 'io.h': No such file or directory`로 실패합니다. 빌드 스크립트가
선제적으로 SDK 헤더를 점검해 명확한 에러를 띄웁니다.

### 빌드 명령

리포지토리 루트에서:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_release.ps1
```

스크립트가 수행하는 일 (`tools\build_release.ps1`):

1. `vswhere`로 `vcvars64.bat`을 찾아 MSVC 환경 초기화
2. `build/`, `dist/`, `build_secure/`, `release_dist/` 정리
3. `python -m compileall src` 로 파싱/컴파일 점검
4. `src/*.py` → `build_secure/protected_src/` 로 미러링
5. `tools/cython_setup.py` 로 각 모듈을 `.pyd` 로 컴파일
6. `build_secure/protected_src/` 의 원본 `.py`/`.c` 삭제 → `.pyd`만 남김
7. `tools/release.spec` 으로 PyInstaller onedir 빌드
8. `dist/DFOGANG_RaidHelper/` 안에 `app/party_apply/general_ocr/...` 등
   프로젝트 모듈의 평문 `.py`가 없는지 검증
9. `release_dist/DFOGANG_RaidHelper_v1.0beta.zip` 으로 압축

### 산출물 경로

| 경로 | 내용 |
| --- | --- |
| `dist/DFOGANG_RaidHelper/DFOGANG_RaidHelper.exe` | 실행 EXE |
| `dist/DFOGANG_RaidHelper/_internal/` | PyInstaller 런타임 + `.pyd` + 리소스 |
| `release_dist/DFOGANG_RaidHelper_v1.0beta.zip` | 사용자 배포용 ZIP |

### 검증 체크리스트

빌드 후 수동으로 확인해야 하는 항목:

- [ ] `python src/gui_app.py` 가 개발 모드에서 정상 실행됨
- [ ] `dist/DFOGANG_RaidHelper/DFOGANG_RaidHelper.exe` 가 실행됨
- [ ] 컨트롤 윈도우에서 START 버튼으로 오버레이가 떠야 함
- [ ] 게임에서 파티 신청창을 띄웠을 때 행이 인식되고 빈 슬롯은 거부됨
- [ ] dfogang 점수가 정상 표시됨
- [ ] 모바일 OCR 프로필이 기본값으로 사용됨 (`general_ocr.py`
      `DEFAULT_OCR_PROFILE = "mobile"`)

빌드 스크립트는 `dist/` 안에 프로젝트 모듈명과 동일한 이름의 `.py`가
있는지 자동 검사하고, 발견 시 빌드를 실패시킵니다.

---

## 3. 트러블슈팅

### "Could not find vcvars64.bat"

Visual Studio Installer를 열어 "Desktop development with C++" 워크로드와
Windows 10 또는 11 SDK 컴포넌트를 추가 설치하세요.

### Cython 단계가 `'io.h': No such file or directory` 로 실패

위와 동일한 SDK 컴포넌트 누락. 빌드 스크립트의 `[0/8]` 단계가 동일한
조건을 미리 검사합니다.

### Cython 환경 구축이 어렵다면 (임시 우회)

v1.0beta는 Cython `.pyd` 보호를 기본 전략으로 채택했습니다. 환경 문제로
Cython 단계만 우회하고 싶다면 `tools\release.spec`을 직접 호출해서
`build_secure/protected_src/` 를 비운 채 빌드하면 PyInstaller가 src를
PYZ 안에 `.pyc`로 컴파일해 넣습니다 (소스 노출은 막을 수 있지만 보호
강도는 더 약함). 다만 정식 v1.0beta 배포용은 Cython 경로로 빌드하세요.

### 사용자 환경에서 PaddleOCR 모델 다운로드가 막혔을 때

PaddleOCR은 `~/.paddleocr/` 또는 `%USERPROFILE%\.paddleocr\` 에 모델을
캐시합니다. 네트워크가 차단된 환경에서는 동일 폴더를 미리 복사해
오프라인 설치를 흉내낼 수 있습니다.
