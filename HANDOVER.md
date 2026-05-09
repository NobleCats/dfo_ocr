# dfo_ocr — Party Apply Detection / Recognition Handover

작성일: 2026-05-09
작성자 (직전 작업): Claude
GitHub: https://github.com/NobleCats/dfo_ocr (main, commit `9ae58a1` "v6" + 후속 수정 다수, 아직 push 전)

---

## 1. 프로젝트 한 줄 요약

DFO 글로벌 클라이언트 화면을 캡처해 **파티/레이드 신청창**을 인식하고, 신청자별 fame · 직업 · 닉네임을 읽어 **dfogang 점수**를 화면 위에 오버레이로 띄우는 데스크톱 도구.

목표:
1. 어떤 해상도/UI Scale에서도 동작
2. 신청 발생 후 **1초 안에** 점수 오버레이
3. 창을 닫았다 다시 열거나 드래그해도 끊김 없이 추적
4. 첫 OCR/API 로딩 후엔 매 프레임이 100ms 내외로 끝나야 함

---

## 2. 현재 동작하는 것 ✅

- **PaddleOCR 3.5.0 / PP-OCRv5** 통합 완료. raw 픽셀 그대로 던지면 정확. 전처리 (Otsu, Lanczos upscale)는 오히려 정확도를 망친다는 것 확인.
- **Multi-marker detection**: `markers/party_apply/column_header_{0,50,69,100}pct.png` 4종. UI Scale 0%/50%/69%/100% 모두 score=1.00으로 검출 성공 (saved frame 기준).
- **Neople API**: jobs `next` chain 재귀 파싱으로 `Neo: <X>` awakening jobGrowId 정확히 인덱싱. `resolve_candidates`가 `(fame, ocr_class)` → 후보 단일 API 호출로 단순화됨. 캐시 hit 시 즉시 반환.
- **dfogang backend**: `dps_normalized` (legacy) → `max_dps_normal` / `dps_normal` 로 교체. score_display 소수점 2자리 (`6.85M`).
- **Async 파이프라인**: API/dfogang 결과 도착 시 `refresh_overlay` Qt signal로 다음 capture tick 안 기다리고 즉시 오버레이 갱신.
- **GUI LOADING → RUNNING 상태 전환**: 첫 frame 처리 완료 시점에 자동 전환.
- **마커 hint mode**: 같은 위치 + 같은 scale인 다음 frame 검출 ~30ms.

샘플 회귀 (`samples/party_apply_01..03.png`): 모든 fame/class/name OCR 정상.

---

## 3. 현재 풀리지 않은 핵심 문제 🔴

**라이브 환경에서 매 frame `recog=` 가 1.0~2.0초로 폭주**.

### 원인 (방금 라이브 로그로 확인)

`logs/log.txt` (방금 받은 로그, UI Scale 0% 라이브) 발췌:

```
09:57:00.925 DEBUG row 10 gate PASS  bright=2867  transitions=262
09:57:01.255 DEBUG row 11 gate PASS  bright=3491  transitions=148
09:57:01.694 DEBUG party_apply frame  cap=22  det=28  recog=770  total=820ms  found=True  rows=0
```

- `det.rows_top_y`는 marker 아래로 12행을 무조건 생성 (각 행 = 56px @ scale 1.0)
- UI Scale 0% (scale=0.66, pitch=37px)에서 마커가 화면 위쪽이면 row 10/11 위치는 **화면 아래쪽 게임 UI 영역** (chat, tooltip, taskbar)
- 그곳의 게임 UI 텍스트가 brightness/transition 게이트를 통과 (`bright=2300-3500, transitions=130-280`)
- 통과 → **PaddleOCR fame + class 호출 (~150ms × 2)** + 템플릿 name/adventure → 행당 ~350ms
- 매 frame row 10, 11 두 행이 OCR 통과 → frame 700ms~1.7s

### 함께 저장된 라이브 프레임

`samples/party_apply_live_0pct_slow_recog.png` — 이 frame 띄워보면 화면 곳곳에 게임 UI 텍스트가 있어서 row 10/11이 그쪽을 가리킴.

### 핵심 측정값 (다양한 콘텐츠 vs 게이트)

| 콘텐츠 | bright | transitions |
|---|---|---|
| 진짜 party_apply 텍스트 행 (sample_03 @ scale=1.0) | 1700~1956 | **886~1028** |
| 진짜 party_apply 추정 (0% UI, scale=0.66로 환산) | ~700~1000 | **~500~650** |
| 게임 UI chat/tooltip (라이브 row 10/11 @ 0% UI) | 2300~3500 | **128~280** |
| 빈 dark 배경 | 0~20 | 0~20 |

→ **transitions만으로 충분히 분리 가능**. 현재 임계값 30. 개선 방향: **400** 정도로 올리면 게임 UI 잘림, 진짜 텍스트는 모두 통과.

---

## 4. 즉시 적용 가능한 fix (간단)

### 옵션 A — 가장 빠른 fix (5분 작업)

`src/party_apply.py` `recognize_party_apply` 안의 transition 임계값을 30 → 400 으로 올리기:

```python
if transitions < 400:   # ← 30에서 400으로
    empties_since_real += 1
    ...
```

검증 데이터:
- sample 03 @ scale=1.0 모든 행 transitions ≥ 826 → 통과 ✓
- 라이브 0% UI 게임 chat 행 transitions ≤ 280 → 거절 ✓
- 100% UI 진짜 텍스트 (scale=1.30): ~1300+ transitions 예상 → 통과

다만 0% UI 진짜 텍스트가 ~500-650으로 추정되는 것이 약간 위험. 직접 실측 안 됨 (0% UI에서 실제 신청 받은 frame이 없음). 사용자에게 0% UI에서 실제 신청 받은 상태로 saved frame 한 장 더 요청해 측정 후 임계값 fine-tune 권장.

### 옵션 B — 더 견고한 fix (장기)

**창의 실제 y-bottom을 검출**해 row scan을 그 안으로 제한. 게임 UI가 어디 있든 영향 안 받음.

방법:
1. marker 아래 column 한 줄 (예: `mx + mw // 2`) 의 brightness 프로파일 스캔
2. 일정 길이 (예: marker_y+30 ~ marker_y+50) 를 "window background baseline" 으로 학습 (mean ~10, std ~5)
3. 아래로 내려가며 mean/std가 baseline에서 크게 벗어나는 첫 y를 window bottom으로 채택
4. `det.rows_top_y` 를 그 이하 y만 포함하도록 필터

레퍼런스 데이터 (saved 0% UI):
- marker (319, 455), 484×10
- column x=561 brightness: y=467-660+ 까지 mean ~9-13, std ~5 (안정적)
- 즉 window body가 200px+ 뻗어 있음 → 단순 std-jump 검출은 어려움

실제로는 더 정교한 신호 필요 — 창 우측 아래의 "Remove Declined Requests" 버튼 (밝은 빨강/주황) 검출이 나을 수 있음.

### 옵션 C — Hard cap (이미 적용됨)

`MAX_OCR_PER_FRAME = 8` 이미 적용. 8행 이상 OCR 시도 막아서 worst case ~1.2s로 cap. Lower bound는 옵션 A/B로 추가 단축 필요.

---

## 5. 아키텍처 / 파일 맵

```
src/
├── app.py            ← LiveDemo: 캡처 루프, frame_emitter, async API/dfogang
│                       PaddleOCR prewarm, ready_callback, 마커 hint+near_scale 캐시
├── party_apply.py    ← detect_party_apply (multi-marker, hint, near, coarse+fine)
│                       recognize_party_apply (per-row gate + OCR pipeline)
├── general_ocr.py    ← PaddleOCR 3.x 래퍼 (read_fame, read_class, read_text_boxes)
│                       brightness pre-check (_has_text), bytes-key 결과 cache
├── neople.py         ← Neople DFO API 클라이언트 (jobs/characters-fame/character)
│                       jobs() 재귀 walk 'next' chain, dual-key index
├── dfogang.py        ← dfogang.com 점수 클라이언트, 스코어 캐시 + 인플라이트 dedup
├── gui_app.py        ← Qt control window, debug log viewer, Save Frame 버튼
├── overlay.py        ← 투명 클릭-쓰루 오버레이 윈도우
├── recognize.py      ← 템플릿 라이브러리 로더, raid_party 전용 (party_apply는 우회)
└── (raid_party*는 deprecated 모드, mode="party_apply" 만 사용)

markers/party_apply/
├── column_header_69pct.png    ← reference (734×16)
├── column_header_0pct.png     ← 484×10
├── column_header_50pct.png    ← 646×13
└── column_header_100pct.png   ← 954×19

samples/
├── party_apply_01.png ~ 03.png   ← @ UI 69% (이전 작업의 ground truth)
└── party_apply_live_0pct_slow_recog.png  ← 방금 추가, 이번 문제 재현용

logs/log.txt   ← 매 GUI 실행마다 새로 덮어씀, 디버그 진단용
```

---

## 6. 핵심 동작 흐름 (단순화)

```
[Capture Loop @ 250ms]
  ↓
detect_party_apply(frame, hint, near_scale)
    ├ hint 있음 → ROI 내 빠른 매칭 (~30ms)
    ├ hint 실패 + near_scale 있음 → 가장 가까운 base_scale 마커로 native 우선 시도 (~100ms)
    └ 둘 다 실패 → 4 markers 모두 coarse(s=1.0 강제 포함) → fine refinement (~2-5s, 첫 1회만)
  ↓
recognize_party_apply(frame, det, templates, digit_t)
    for each row in det.rows_top_y (max 12):
        ├ 빈행 게이트 (brightness + transitions, 현재 임계값 20/30) ← 너무 느슨함, 본 문서 §3 참조
        ├ MAX_OCR_PER_FRAME=8 cap
        └ 통과 시:
            ├ _read_fame    (PaddleOCR digits ~150ms, _has_text 사전 체크)
            ├ _read_text(name)  (templates 빠름)
            ├ _read_class   (PaddleOCR alpha ~150ms)
            └ _read_text(adventure) (templates 빠름)
  ↓
_apply_party_apply_result (Qt main thread)
    for each row:
        ├ 같은 (fame, class_norm, name_norm) 캐시 hit이면 skip
        └ 미스면 worker thread에 _fetch_pa_candidates 제출
            ├ neople.resolve_candidates(fame, ocr_class, ocr_name)
            │   - match_jobs(class) → Neo: 우선 JobInfo
            │   - search_by_fame(jobId, growId, fame, window=100)
            │   - sim ≥ 0.7 필터 후 top 후보 채택
            ├ 성공 시 _pa_resolve_cache[key] = canonical (영구)
            ├ 실패 시 _pa_resolve_cache[key] = None (캐시 안 함, 다음 프레임 재시도)
            └ canonical 있으면 즉시 dfogang.get_info(canonical) submit
                └ 결과 도착 시 → refresh_overlay signal → 다음 tick 안 기다리고 즉시 오버레이 갱신
```

---

## 7. 핵심 상수 (자주 손대는 것들)

| 위치 | 상수 | 현재값 | 의미 |
|---|---|---|---|
| `gui_app.py` | `DEFAULT_CAPTURE_INTERVAL_MS` | 250 | 캡처 주기 |
| `app.py` | `COLD_SCAN_MIN_INTERVAL_S` | 0.3 | cold scan 최소 간격 |
| `app.py` | `PARTY_APPLY_REF_UI_PCT` | 69.0 | reference UI Scale (= scale 1.0) |
| `app.py` | `PARTY_APPLY_MIN_CLASS_CONF` | 0.4 | class OCR conf 컷오프 |
| `party_apply.py` | `MAX_OCR_PER_FRAME` | 8 | frame당 최대 OCR-시도 행 (worst-case cap) |
| `party_apply.py` | gate `bright_count` | 20 | 빈 행 brightness 임계값 |
| `party_apply.py` | gate `transitions` | **30** | **빈 행 transition 임계값 ← 이번 문제의 핵심, 400으로 권장** |
| `party_apply.py` | `TEMPLATE_SCALE_FOR_PARTY_APPLY` | 0.7 | name/adv 템플릿 스케일 multiplier |
| `party_apply.py` | `TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS` | 0.5 | digits 템플릿 multiplier (현재 fame은 OCR 우선이라 거의 안 씀) |
| `party_apply.py` | `FAME_STAR_ICON_RIGHT_PAD` | 22 | 별 아이콘 영역 스킵 |
| `party_apply.py` | `FAME_DIGIT_LEFT/RIGHT_BREATHING` | 6 / 42 | OCR fame crop padding |
| `general_ocr.py` | `_FAME_TYPICAL_MIN/MAX` | 30k / 200k | parser typical band 우선 선택 |
| `general_ocr.py` | `_FAME_MIN/MAX` | 10k / 999k | parser 절대 허용 범위 |
| `neople.py` | `name_min_similarity` | 0.7 | candidate name 유사도 임계값 |
| `neople.py` | `commit canonical` 임계값 (`app.py`) | 0.7 | sim ≥ 0.7 일 때만 canonical 등록 |

---

## 8. 검증된 핵심 결정사항 (다시 뒤집지 말 것)

| 결정 | 이유 |
|---|---|
| **fame은 OCR 우선** (templates는 fallback) | 템플릿 매칭이 던파 fame 폰트 변형에 너무 약함 (수많은 iteration 끝 결론) |
| **PaddleOCR raw 픽셀 입력** (Otsu/upscale 금지) | 전처리가 6/5, 8/3 등 자릿수 구분 anti-aliasing 정보 파괴 |
| **전체 화면 OCR 금지** (window 탐지용) | v4 시도했다가 매 frame 3-4초로 망함. multi-marker template anchor가 표준 |
| **`Neo:` prefix 보존** (norm_jobname 에서 strip 금지) | 글로벌 99.9% 캐릭터가 Neo tier — 분리 매칭 안 하면 wrong jobGrowId |
| **jobs() 'next' chain 재귀 파싱 필수** | Neople API가 awakening 순서를 nested next로 줌. 이거 안 하면 Neo: 항목 자체가 인덱스에 없음 |
| **dps_normalized 응답에서 제거** | 사용자 정책: legacy. `max_dps_normal` (또는 `dps_normal`) 만 사용 |
| **score_display 소수점 둘째자리** | 사용자 요구 (예: `6.85M`) |
| **성공한 (fame,class,name) 영구 캐시 / 실패는 비캐시** | 사용자 명시 요구 |
| **OCR name과 별도 stable_key 미구현 상태** | OCR name이 매 프레임 흔들리면 같은 row 반복 매칭 위험. 핸드오버 이전 작업의 미해결 항목 |

---

## 9. 사용자가 명시한 제약 (절대 어기지 말 것)

1. fame OCR이 제일 어렵다는 사실. template digit matching 회귀 금지.
2. 전체 화면 OCR로 창 탐지하는 v4 방식 절대 금지. anchor template + grid validation 유지.
3. 점수가 표기되는 데까지 1초 안에 들어와야 함 (초기 PaddleOCR 모델 로드 후).
4. 모든 직업명은 `Neo: <X>` 마지막 jobGrow 레벨로 가정 (글로벌 endgame 99.9%).
5. 성공 응답은 영구 캐시 (재요청 금지). 실패는 다음 프레임 재시도 허용.
6. 게임이 300fps라 캡처 주기 절대 1초 미만으로 너무 빨리 잡지 말 것 (현재 250ms).

---

## 10. 다음 작업자가 우선 처리할 항목 (우선순위 순)

### P0 (지금 바로)
- [ ] `party_apply.py` recognize_party_apply 안의 `if transitions < 30` 을 `< 400` 으로 변경. 사용자 라이브 테스트 후 (0% UI에서 실제 신청 받은 상태) 진짜 텍스트 transitions 측정해 fine-tune.
- [ ] 0% UI에서 **실제 신청자가 1명 이상 있는 saved frame** 확보. samples/ 에 추가.

### P1 (며칠 안)
- [ ] **창 y-bottom 자동 검출**: §4 옵션 B 구현. 또는 "Remove Declined Requests" 버튼 detect.
- [ ] **stable cache key**: `(fame, class_norm)` 만으로도 성공 캐시 조회 (OCR name 흔들림 무시). 실패는 여전히 `(fame, class, name)` 풀 키로만.
- [ ] **fame 100k+ 대비**: 현재 parser의 `_FAME_TYPICAL_MAX=200k` 가 곧 100k 돌파하면 좁아질 수 있음. 200k 넘는 케이스 실측 시 typical band 재조정.

### P2 (장기)
- [ ] **실제 grid line 기반 column boundary 검출** (이전 핸드오버 문서 권장). 현재는 reference scale × multiplier 방식. UI Scale 변화에 마커-anchor + scale 곱으로 대처 중인데, 장기적으로는 detected vertical/horizontal grid line이 더 견고.
- [ ] 빌드/배포: PyInstaller spec (`DFOGANG Raid Helper.spec`) PaddleOCR/Paddle/Torch hidden imports 추가 완료. 실제 dist 빌드 검증은 안 했음.
- [ ] **Async OCR**: capture loop가 OCR 동안 block되지 않도록 OCR worker 분리. 현재는 `_process_frame_party_apply` 안에서 OCR 동기 호출 → frame loop 자체가 1.5s씩 멈춤.

---

## 11. 알아두면 좋은 함정들

1. **`detect_party_apply`의 `det.scale`은 effective scale** (= resize × marker base_scale). marker_xywh의 width로 base_scale 역산 가능. 마커별 base_scale: 0pct=0.659, 50pct=0.880, 69pct=1.000, 100pct=1.300.
2. **coarse 스캔에 `s=1.0` 명시 포함 필수**: 마커가 native 크기일 때 s=1.0이 score=1.0 보장. step=0.1로 [0.95, 1.05]만 시도하면 점수가 0.25까지 떨어짐 (template match는 5% resize에도 매우 민감).
3. **`_active_template_scale` flip-flop**: 매 frame `_get_templates_for_scale` 가 alpha_scale 다음 digit_scale로 두 번 호출되면서 `_row_cache` 가 reset됨. raid_party 모드 잔재라 party_apply에는 영향 없지만 정리 필요.
4. **dfogang `score_display` 직접 신뢰 금지** — 백엔드 코드 (`C:/MesugakiProto/dfogang_backend/api/routes/realtime.py`)의 `_pick_score` 가 `max_dps_normal` 우선 픽업하도록 수정해 둔 상태. 백엔드 재시작 필요.
5. **modelscope 스텁 + OneDNN 비활성화**: `general_ocr.py` import 시점에 `os.environ['FLAGS_use_mkldnn']='false'` 설정 + `sys.modules['modelscope']` stub. 안 하면 paddle 3.x가 torch DLL 충돌 OR `ConvertPirAttribute2RuntimeAttribute not support` 로 죽음.
6. **`_FrameEmitter.refresh_overlay` Qt signal**: 워커 스레드에서 캐시 갱신 후 emit. `_refresh_overlay_from_cache` 슬롯이 `_last_pa_result` 스냅샷 기반으로 즉시 오버레이 재구성. 다음 capture tick까지 기다리지 않음.
7. **로그**: 모든 `dfogang.*` logger가 `dfogang` 부모로 propagate, 거기에 file handler 부착. `logs/log.txt`는 매 GUI 실행 새로 덮어쓰기. `%LOCALAPPDATA%/DFOGANG_RaidHelper/debug.log` 는 rotating 누적.
8. **사용자는 `/loop` 또는 `/schedule` 사용 안 함** (이 프로젝트와 무관).

---

## 12. 디버그 워크플로우

1. `python src/gui_app.py`
2. Neople API key 입력
3. ▶ 누르고 "D" 버튼으로 디버그 창 열기
4. 게임에서 시나리오 재현 (창 열기/이동/닫기 등)
5. 디버그 창의 "Save Frame" 버튼 클릭 (또는 `%LOCALAPPDATA%/DFOGANG_RaidHelper/last_frame.png` 직접 회수)
6. `logs/log.txt` 확인
7. 필요시 `cd C:/Users/Noble/Desktop/dfo_ocr; python` 으로 saved frame에 detect_party_apply / recognize_party_apply 직접 호출해서 재현

진단용 코드 스니펫:

```python
import sys; sys.path.insert(0, 'src')
import numpy as np
from PIL import Image
from party_apply import (detect_party_apply, recognize_party_apply,
                         TEMPLATE_SCALE_FOR_PARTY_APPLY,
                         TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS)
from recognize import load_default_templates

img = np.array(Image.open('samples/party_apply_live_0pct_slow_recog.png').convert('RGB'))
det = detect_party_apply(img)
print(det.found, det.score, det.scale, det.marker_xywh)
templates = load_default_templates(ui_scale=TEMPLATE_SCALE_FOR_PARTY_APPLY * det.scale)
digit_lib = load_default_templates(ui_scale=TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS * det.scale)
digit_t = {ch: v for ch, v in digit_lib.items() if ch.isdigit() or ch == ','}
rows = recognize_party_apply(img, det, templates, digit_t)
for r in rows:
    print(r)
```

---

## 13. 환경 정보 (현재 사용자 머신)

- Windows 11, Python 3.10.8
- PaddleOCR 3.5.0, PaddlePaddle 2.6.2 (3.3.1으로 한 번 올렸다가 OneDNN 호환 문제로 다운그레이드)
- protobuf 3.20.2 (paddlepaddle 2.6.2 요구)
- PyTorch 2.7.0+cu128 (EasyOCR fallback용)
- EasyOCR 1.7.2 (보조 fallback)
- mss (스크린 캡처)
- PyQt6
- 기타: opencv-contrib-python, numpy, requests
- DFO 클라이언트는 보더리스 모드, native 캡처 해상도 2134×1200

---

## 14. 마지막 사용자 메시지

> "현재 작업은 chatGPT로 마저 작업하겠습니다."

본 문서가 그 인수인계.
