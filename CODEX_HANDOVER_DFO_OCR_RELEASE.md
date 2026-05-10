# Codex CLI 인수인계 문서 — DFO OCR / DFOGANG Raid Helper 릴리즈 빌드

작성일: 2026-05-09  
대상 repo: `NobleCats/dfo_ocr`  
기준 커밋: `1eb77268f4fadb934ed6e1ad5b85eb2a56b1e578`  
현재 목표: 일반 유저가 별도 Python/pip/bat 없이 실행 가능한 배포 파일 생성

---

## 1. 프로젝트 상태 요약

이 프로젝트는 DFO 레이드/파티 신청창을 감지하고, 신청자 정보를 OCR로 읽은 뒤 Neople API 및 DFOGANG 점수를 조회하여 overlay에 표시하는 Windows GUI 앱이다.

개발 실행 기준은 다음이다.

```powershell
python .\src\gui_app.py
```

개발 실행에서는 현재 주요 기능이 동작한다.

확인된 기능:

- UI Scale 0~100% 가변 대응
- 파티 신청창 이동/닫힘/재열림 대응
- row OCR cache
- 빈 슬롯 no-action gate
- server/default OCR 기본값
- mobile OCR은 opt-in
- `Neo: Crusader` 같은 중복 직업명은 Priest(M/F)를 모두 검색
- partial fame prefix fallback
- exact fame 기반 candidate accept 완화

---

## 2. 중요 기능 이슈와 수정 히스토리

### 2.1 NeNeSan 이슈

문제:

- `NeNeSan`은 `Priest (F) / Neo: Crusader`
- OCR name이 `MeMeSa:`처럼 잘못 읽히는 경우가 있음
- fame/class는 정확히 읽힘:
  - fame: `74,733`
  - class: `BNeo: Crusader`
- 기존 resolver는 Priest(M)만 검색하거나, Priest(M/F)를 모두 검색하더라도 name similarity threshold 때문에 reject함

수정 방향:

- `Neo: Crusader`는 Priest(M), Priest(F) 모두 검색해야 함
- exact fame이면 `fame=[74733..74733]` 단일값 검색을 먼저 수행해야 함
- exact fame + class/grow match가 강하면 name OCR이 약해도 accept해야 함
- 이후 실패할 때만 기존 `±100` fallback으로 이동

현재 `src/neople.py`에는 이 방향의 수정이 들어가 있음.

확인해야 할 로그 키워드:

```text
resolve_exact_fame_search
accept_exact_fame_unique_candidate
accept_exact_fame_top_candidate
fallback_to_fame_window
class_ambiguous
```

정상 기대 흐름:

```text
resolve_exact_fame_search class='BNeo: Crusader' name='MeMeSa:' fame=74733
class_ambiguous ... Priest (M)/Neo: Crusader, Priest (F)/Neo: Crusader
searched Priest (M)/Neo: Crusader [74733..74733] → ...
searched Priest (F)/Neo: Crusader [74733..74733] → ...
accept_exact_fame_unique_candidate ... canonical=NeNeSan ...
```

---

## 3. OCR 상태

현재 기본 OCR은 mobile이 아니라 server/default 계열이어야 한다.

정상 로그:

```text
PaddleOCR reader initialized (PP-OCRv5 profile=server rec_model=server_default)
```

mobile은 빠르지만 `NeNeSan -> MeMeSa` 같은 이름 OCR 문제를 악화시킬 수 있으므로 기본값으로 쓰면 안 된다.

환경변수 사용:

```cmd
set DFO_OCR_PROFILE=mobile
python .\src\gui_app.py
```

기본값 복귀:

```cmd
set DFO_OCR_PROFILE=
set DFO_OCR_RECOGNITION_MODEL=
python .\src\gui_app.py
```

---

## 4. 배포 시도 히스토리

### 4.1 PyInstaller onedir + Cython protected build

한때 `tools/build_release.ps1`, `tools/release.spec`, `tools/cython_setup.py` 기반으로 onedir ZIP 빌드를 시도했다.

장점:

- 내부 `src/*.py`를 Cython `.pyd`로 보호 가능
- ZIP 배포 가능

문제:

- 사용자가 이전 dist/ZIP을 실행하는지 최신 빌드를 실행하는지 혼선 발생
- `gui_app.py` 실행과 release EXE 동작이 다르게 보이는 상황 발생
- 배포물 추적이 복잡함

현재 방향에서는 우선순위 낮음.

---

### 4.2 Dual-EXE 구조 시도

시도한 구조:

```text
DFOGANG_Runtime_Setup_v1.exe
  - PyQt6, PaddleOCR, PaddlePaddle, OpenCV 등 dependency 설치
  - %LOCALAPPDATA%\DFOGANG_RaidHelper\runtime_v1

DFOGANG_RaidHelper_v1.0beta_<commit>.exe
  - 작은 앱 EXE
  - 외부 runtime site-packages를 sys.path/DLL path에 추가
```

장점:

- runtime installer는 약 199.4MB까지 줄어듦
- app EXE는 약 6.6MB까지 줄어듦
- 업데이트 시 app EXE만 배포 가능

문제:

- Windows DLL 로딩이 불안정
- PyQt6 `QtCore` DLL 로딩 실패
- `pkgutil` 누락
- `runtime.json` BOM 문제
- 외부 site-packages + PyInstaller onefile 조합이 취약함

마지막 실패:

```text
ImportError: DLL load failed while importing QtCore: The specified module could not be found.
```

판단:

- 이 방식은 이론적으로 가능하지만 현재 릴리즈 직전 단계에서 안정화 비용이 큼
- 우선 중단 권장

---

### 4.3 Fully bundled onefile EXE 시도

현재 마지막 방향.

목표:

```text
DFOGANG_RaidHelper_v1.0beta_<commit>.exe
```

사용자는 이 파일 하나만 실행한다.

장점:

- Python 설치 불필요
- pip 불필요
- bat 불필요
- runtime installer 불필요
- 폴더 구조 없음
- 배포/테스트 대상이 단일 파일이라 혼선 감소

단점:

- 매우 큼
- 첫 실행 느림
- PyInstaller onefile 특성상 temp extraction 발생
- PyInstaller hook dependency 문제가 계속 나올 수 있음

현재 제공/추가된 파일:

```text
RELEASE_FULL_ONEFILE.md
tools/torch_stub_runtime_hook.py
tools/launcher_full_onefile.py
tools/cython_setup_full_onefile.py
tools/full_onefile.spec
tools/build_full_onefile_release.ps1
```

빌드 명령:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_full_onefile_release.ps1
```

현재 마지막 실패:

```text
Traceback (most recent call last):
  File "pyi_rth_nltk.py", line 14, in <module>
  ...
  File "nltk\util.py", line 11, in <module>
ModuleNotFoundError: No module named 'pydoc'
```

이 에러는 PyInstaller가 `nltk` runtime hook을 포함했고, spec에서 `pydoc`을 exclude했기 때문에 발생했다.

우선 수정 후보:

1. `tools/full_onefile.spec`의 excludes에서 `pydoc` 제거
2. 또는 `hiddenimports`에 `pydoc` 추가
3. 가능하면 `nltk` 자체를 exclude하거나, 왜 `nltk`가 끌려오는지 추적
4. PaddleOCR/PaddleX/ModelScope/HuggingFace 쪽이 nltk를 끌어오는지 확인
5. 불필요하면 다음 excludes 추가 검토:
   - `nltk`
   - `datasets`
   - `transformers`
   - `tokenizers`
   - 단, PaddleOCR import 경로가 깨지지 않는지 확인 필요

---

## 5. 현재 full onefile spec의 핵심 구조

`tools/full_onefile.spec`는 다음을 목표로 한다.

- Cython으로 보호된 `.pyd` 모듈 포함
- resources/templates/markers 포함
- PyQt6, PaddleOCR, PaddlePaddle, PaddleX, OpenCV 포함
- torch는 제외하고 `tools/torch_stub_runtime_hook.py`로 stub 처리
- TensorFlow/torch/matplotlib/scipy/sklearn 등 대형 optional dependency 제외

현재 spec에서 주의해야 할 점:

```python
excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
    "tensorboard",
    "onnx",
    "onnxruntime",
    "openvino",
    "tensorrt",
    "paddle.tensorrt",
    "IPython",
    "jedi",
    "notebook",
    "jupyter",
    "matplotlib",
    "seaborn",
    "sklearn",
    "scipy",
    "pytest",
    "unittest",
    "pydoc",
]
```

`pydoc`는 제거해야 할 가능성이 높다. `nltk` hook이 pydoc을 필요로 해서 현재 앱이 시작 전에 죽는다.

권장 1차 수정:

```python
# excludes에서 제거
"pydoc",
```

또는:

```python
hiddenimports += ["pydoc"]
```

더 나은 2차 수정은 `nltk`가 정말 필요한지 조사하는 것이다.

---

## 6. 추천 작업 순서 for Codex

### Step 1. 현재 상태 정리

```powershell
git status
git rev-parse HEAD
git log --oneline -5
```

기준 커밋 또는 branch가 `1eb77268f4fadb934ed6e1ad5b85eb2a56b1e578` 이후인지 확인한다.

현재 사용자 요청은 "1eb7726 기반으로 full onefile 릴리즈를 안정화"이다.

---

### Step 2. dev 실행 기준 기능 확인

```powershell
python -m compileall src
python .\src\gui_app.py
```

확인:

- GUI 실행
- 버전 표시
- debug button 없음
- OCR 로그가 `profile=server rec_model=server_default`
- 가능하면 NeNeSan 테스트

---

### Step 3. full onefile 빌드 수정

가장 먼저:

- `tools/full_onefile.spec`에서 `pydoc` exclude 제거
- 또는 hiddenimports에 `pydoc` 추가

그 후:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_full_onefile_release.ps1
```

빌드 후 결과:

```text
release_dist\DFOGANG_RaidHelper_v1.0beta_<commit>.exe
```

실행 테스트:

```powershell
.\release_dist\DFOGANG_RaidHelper_v1.0beta_<commit>.exe
```

---

### Step 4. PyInstaller runtime hook 문제 반복 대응

현재 실패 유형은 PyInstaller가 끌어온 runtime hook / optional package 때문이다.

대응 원칙:

1. 앱에서 직접 필요 없는 패키지는 exclude
2. 단, import-time dependency가 깨지면 stub 또는 hiddenimport로 최소 대응
3. torch는 포함하지 않는 방향 유지 권장
4. pydoc 같은 stdlib는 굳이 제외하지 말 것
5. `nltk`, `datasets`, `transformers`가 필요 없다면 exclude 후보

예상 후보 수정:

```python
excludes += [
    "nltk",
    "datasets",
    "transformers",
    "tokenizers",
]
```

하지만 PaddleOCR 3.x / PaddleX / ModelScope import가 이 중 일부를 요구할 수 있으므로, 하나씩 추가하고 실행 테스트해야 한다.

---

### Step 5. EXE 실행 후 OCR/NeNeSan 검증

실행 후 로그 경로:

```text
%LOCALAPPDATA%\DFOGANG_RaidHelper\log.txt
```

확인 명령:

```powershell
Select-String -Path "$env:LOCALAPPDATA\DFOGANG_RaidHelper\log.txt" `
  -Pattern "PaddleOCR reader initialized|resolve_exact_fame_search|accept_exact_fame|NeNeSan|MeMeSa|fallback_to_fame_window|class_ambiguous"
```

정상 조건:

- `PaddleOCR reader initialized (PP-OCRv5 profile=server rec_model=server_default)`
- `class_ambiguous ... Priest (M)/Neo: Crusader, Priest (F)/Neo: Crusader`
- `resolve_exact_fame_search ... fame=74733`
- `accept_exact_fame_unique_candidate` 또는 `accept_exact_fame_top_candidate`
- overlay에 NeNeSan score 표시

---

## 7. 보호/보안 요구사항

배포물에 프로젝트 원본 `.py`가 직접 노출되면 안 된다.

full onefile은 내부적으로 temp extraction을 하지만, 원본 `src/*.py`를 넣지 않고 `.pyd`를 넣는 구조다.

확인할 것:

- `build_secure/protected_src`의 `.py`와 `.c`가 PyInstaller 전에 삭제되는지
- spec이 `src/*.py`를 직접 datas로 넣지 않는지
- `project_modules`는 `.pyd` hidden import로 해결되는지

빌드 스크립트 흐름:

```text
src/*.py -> build_secure/protected_src
build_info.py 생성
Cython .pyd 생성
staged .py/.c 삭제
PyInstaller onefile 빌드
```

이 구조는 유지 권장.

---

## 8. Git 커밋 가이드

추천 커밋 단위:

1. `Fix full onefile PyInstaller runtime imports`
2. `Stabilize bundled onefile release build`
3. `Document full onefile release process`

빌드 산출물은 커밋하지 않는다.

커밋하지 말 것:

```text
build/
dist/
release_dist/
build_secure/
*.pyd
*.c
*.obj
*.pyc
__pycache__/
```

---

## 9. 최종 배포 목표

최종 배포물:

```text
DFOGANG_RaidHelper_v1.0beta_<commit>.exe
```

사용자 안내:

```text
1. EXE 다운로드
2. 실행
3. Windows 보안 경고가 뜨면 "추가 정보 → 실행"
4. Neople API Key 입력 후 사용
```

런타임 설치 EXE / Python / pip / bat / ZIP extraction은 요구하지 않는 방향.

---

## 10. Codex에게 맡길 핵심 결론

현재 외부 런타임 분리 방식은 중단한다.  
full onefile EXE를 안정화한다.  
첫 번째 실패는 `pydoc` exclude 때문에 발생했으므로 `tools/full_onefile.spec`부터 수정한다.  
이후 PyInstaller runtime hook 문제가 나오면 필요한 stdlib hiddenimport 추가 또는 불필요 optional package exclude로 해결한다.  
최종적으로 dev `gui_app.py`와 full onefile EXE의 NeNeSan 결과가 같아야 한다.
