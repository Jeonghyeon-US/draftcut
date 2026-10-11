# Draftcut

[English](README.md) · 한국어

Draftcut은 개발자와 AI 코딩 에이전트를 사용하는 영상 편집자를 위한 도구 모음입니다.
영상에서 관측 정보를 추출하고, 편집 내용을 적은 JSON 문서인 EditPlan(편집 계획서)을
ffmpeg로 실행해 영상 파일을 만듭니다. 비선형 편집 프로그램(NLE)은 아니며,
어디를 자를지 판단하는 일은 에이전트가 맡습니다.

## 가장 빠른 사용법

저장소를 복제한 뒤, 사용하는 AI 코딩 에이전트에게 다음과 같이 요청합니다.

> "이 저장소를 설치해서 `<내 영상>`을 편집해 줘. 편집 목표는 `<목표>`이고 출력 조건은 `<출력 조건>`이야."

에이전트는 [AGENTS.md](AGENTS.md)에 따라 필요한 항목을 설명하고 설치 전에 허가를
받습니다. 이후 영상을 살펴보고 계획서를 작성해 렌더링합니다. 부족한 항목을 직접
확인하려면 다음 명령을 실행합니다.

```bash
python3 check_env.py
```

## 할 수 있는 것

### 1. 영상 관측

Index는 영상 안의 시간 위치를 함께 기록한 관측 목록으로, 편집할 내용을 판단하는 데 사용합니다.

| 관측 내용 | 방법 |
|---|---|
| 장면이 바뀌는 지점 | ffmpeg 장면 전환 검출 |
| 화면이 멈춰 있는 구간 | 정지 구간 검출 |
| 움직임이 많거나 적은 구간 | 프레임 간 차이 측정 |
| 화면 속 글자 | Apple Vision 문자 인식(OCR), macOS 전용 |
| 말한 내용 | 로컬 Whisper 음성 전사, 낱말별 시간 정보 포함 |

시간은 소수점 초 대신 정수 PTS(presentation timestamp, 표시 시각) 틱으로 기록합니다.
틱은 영상의 시간 기준에 따른 단위이며, 반올림 오차가 누적되어 프레임이 밀리는 일을
방지합니다.

### 2. 25가지 편집 동작 유형 실행

| 분야 | 동작 |
|---|---|
| 자르고 붙이기 | 구간 자르기, 이어 붙이기, 순서 변경, 삽입, 삭제 후 빈자리 당기기(리플 딜리트), 구간 반복 |
| 시간 | 배속·슬로 모션, 정지 프레임 |
| 화면 | 크롭·리프레임, 키프레임 줌, 크기·위치 조정, 화면비 변경(16:9 → 9:16 등), 이미지·로고 겹치기 |
| 민감정보 가리기 | 개인정보·계정명·화면 속 민감한 문자열이 있는 영역에 블러 적용 |
| 자막 | 영상에 자막 입히기, 구간별 스타일 지정, 전사 내용에서 자막 초안 생성 |
| 소리 | 라우드니스 정규화, 노이즈 제거, 볼륨 조절, 페이드, 크로스페이드, 말할 때 음악 낮추기(더킹), 믹싱, 배경음악·효과음·환경음 삽입 |
| 출력·관측 | 실행기가 인코딩 출력과 캔버스 설정을 처리하며, 별도 관측 도구가 프레임을 추출합니다 |

실행기의 `actions[]`에는 동작 이름 25개를 넣을 수 있습니다. 레지스트리에는
이 배열에서 받지 않는 작업도 있습니다. `act_export_media`는 실행기의 기본 출력이고,
`act_change_canvas`는 `output_policy`로 설정합니다.
`act_extract_capture_evidence_frames`는 별도 관측 도구에서 처리합니다.
독립적인 `act_remux_media` 렌더 경로는 없습니다. 이 네 이름은 모두 `actions[]`에서
거부됩니다. `contracts/action-executability.json`과 실행기의 지원 범위를 함께
확인해야 하며, 레지스트리 표지만으로 렌더 지원을 판단할 수 없습니다.

### 3. 계획서와 렌더링 결과 검사

| 시점 | 검사 내용 |
|---|---|
| 렌더링 전 | 계획서 형식과 구조 규칙: `validate_plan.py` |
| 렌더링 전 | 실행기의 계획서 내부 검사: `executor/assemble.py --validate-only` |
| 렌더링 전 | 예상 렌더링 시간: `executor/assemble.py --estimate-only` |
| 렌더링 전 | 계획한 가림 사각형이 검출된 영역을 덮는지 확인: `skills/mask-plan/coverage_gate.py` |
| 렌더링 전 | 배속의 안전성과 재인코딩 없이 구간을 이어 붙일 수 있는지 확인 |
| 렌더링 후 | 계획한 가림 사각형에서 샘플링한 픽셀이 임계값 안에서 균일한지 확인: `skills/mask-plan/verify_render.py` |

`validate_plan.py`를 먼저 실행한 뒤 `--validate-only`를 실행합니다. 후자는 계획서의
형식 정의(스키마), 자산 파일이나 해시를 읽지 않고 ffmpeg도 실행하지 않습니다.
`validated` 결과만으로 계획서가 올바르거나 렌더링할 수 있다고 판단할 수 없습니다.

`coverage_gate.py`는 원본 파일 하나만 사용하고, 검출 시각과 모든 계획 구간에
같은 시간 단위(틱)를 사용하는 경우를 전제로 합니다. 원본과 출력 시간이 1:1로
대응해야 하므로 원본 구간 반복과 정지 프레임은 없어야 합니다. 크롭에 따른 좌표
이동 외의 화면 변환도 없어야 합니다. 이 도구는 이러한 전제를 검사하지 않습니다.
첫 번째로 일치하는 클립만 확인하며 크기 변경·패딩·줌은 반영하지 않고, 배속은
거부합니다. 이 조건을 벗어난 PASS는 계획한 가림 사각형이 검출된 영역을 덮는다는
근거가 되지 않습니다. 검출되지 않은 민감정보도 확인하지 못합니다.

### 4. 완성본에서 컷 목록 복원

`skills/pair-align/`의 도구는 오디오 지문으로 원본과 기존 편집본을 정렬해 무엇이
남고 잘렸는지 복원합니다. 입력 오디오·매개변수·소프트웨어 버전이 같으면 정렬
알고리즘은 같은 결과를 냅니다. `plan_from_alignment.py`는 정렬 결과를 EditPlan으로
변환합니다. `trusted: true`인 구간을 선택하며, `trusted`가 없거나 `null`이면
`confidence >= 0.6`인 구간을 선택합니다. `alignment.json`의
`summary.mapped_ratio`(매핑률)는 요약 신뢰 임계값을 통과한 구간들의 편집본 시간
길이 합을 편집본 전체 길이로 나눈 값입니다. 이 값이 0.95(95%) 미만이면 계획서를
만들지 않아야 합니다. 이는 변환기가 자동으로 차단하는 조건이 아니라 사람이 지켜야
할 검토 지침입니다. 이 도구는 기존 편집본을 읽으며 영상을 편집하지 않습니다.

## 할 수 없는 것과 한계

| 지원하지 않는 기능 | 설명 |
|---|---|
| Premiere·Resolve·Final Cut Pro로 내보내기 | OTIO·XML·MLT 내보내기가 없습니다. 출력은 편집 프로젝트가 아닌 영상 파일입니다 |
| 색보정·룩업 테이블(LUT) | 구현되어 있지 않습니다 |
| 멀티캠 앵글 선택 | 구현되어 있지 않습니다 |
| 그래픽 애니메이션 | 구현되어 있지 않습니다 |
| J컷·L컷 | 지원 범위 밖입니다 |
| 자막 텍스트 교체·더빙 | 구현되어 있지 않습니다 |
| 자를 구간을 스스로 판단 | 범용 도구는 포함되어 있지 않으며, 에이전트가 컷을 제안합니다 |
| 지원 플랫폼 밖에서 OCR·음성 전사 | OCR은 macOS의 Apple Vision, 음성 전사는 Apple Silicon이 필요합니다. 나머지 기능은 필수 Python 패키지와 ffmpeg/ffprobe로 사용할 수 있습니다 |

모든 컷 제안은 검토가 필요한 초안입니다. 개발 중 긴 발화 영상 두 편에서
1차 제안은 편집자가 잘라 낸 부분의 일부만 찾았고, 잘못 잘라 되돌려야 하는
구간은 6~17개였습니다. 이 관찰은 이 저장소로 재현할 수 없습니다. 원본 영상과
개발 기록이 포함되어 있지 않습니다. 계획서를 완성본으로 삼기 전에 모든 컷 제안을
검토해야 합니다.

## 설치

먼저 실행 환경을 확인합니다. 이 스크립트는 부족한 항목을 알려 주며 아무것도 설치하지 않습니다.
필수 모듈과 실행 파일의 존재를 확인하지만, 패키지 버전·인코더·필터 지원이나
계획서의 렌더링 가능 여부는 검사하지 않습니다.

```bash
python3 check_env.py          # 사람이 읽는 표
python3 check_env.py --json   # 설치 명령을 포함한 기계 판독용 출력
```

필수 의존성은 Python 3.10+, numpy, Pillow, ffmpeg/ffprobe입니다.

```bash
pip install -r requirements.txt     # numpy와 Pillow
brew install ffmpeg                 # macOS 예시; ffmpeg와 ffprobe는 저장소에 포함되지 않습니다
```

OCR(PyObjC, macOS)과 음성 전사(mlx-whisper, Apple Silicon)는 실행기에 선택 기능이지만,
아래 Index 생성 흐름에는 OCR이 필요합니다. `build_index.py`는 `--ocr-json`을
필수로 받으며 이 빌더는 OCR 없이 Index를 만드는 경로를 제공하지 않습니다. 다른 플랫폼에서는
해당 플랫폼의 패키지 관리자로 ffmpeg/ffprobe를 설치하고, 기존의 유효한 Index와
EditPlan을 실행기에 제공해야 합니다. `DRAFTCUT_FFMPEG`와 `DRAFTCUT_FFPROBE`는
환경 점검기, 실행기, `skills/mask-plan/verify_render.py`에서 읽습니다. 관측·정렬 도구는
PATH의 `ffmpeg`와 `ffprobe`를 사용하므로 해당 디렉터리도 PATH에 추가해야 합니다.

### 실행·송신·다운로드

Draftcut은 로컬 ffmpeg·ffprobe 프로세스를 실행해 영상을 분석하고 처리합니다.
저장소 코드에는 영상·음성·전사 내용을 네트워크 서비스로 업로드하는 호출이 없습니다.
선택 기능인 음성 전사는 로컬 미디어 파일과 모델 경로 또는 Hugging Face 저장소를
mlx-whisper에 전달합니다. 이 라이브러리는 로컬 전사에 사용할 모델 가중치를 내려받을
수 있으며, 모델 가중치는 저장소에 포함되어 있지 않습니다. 위 설치 명령은 Python
패키지와 ffmpeg/ffprobe를 별도로 내려받습니다.

## 직접 해 보기

에이전트에게 맡길 때는 [AGENTS.md](AGENTS.md)를 따릅니다. 아래 명령은 저장소 루트에서
실행하며, 이 Index 생성 흐름에는 OCR을 위한 macOS와 PyObjC가 필요합니다.

```bash
# 1. 영상 관측
python3 skills/media-index/scene_detect.py --input a.mp4 --output scene.json
python3 skills/media-index/ocr_vision.py --input a.mp4 --output ocr.json --frames-dir frames/
python3 skills/media-index/activity_mad.py --input a.mp4 --output activity.json
python3 skills/media-index/build_index.py --input a.mp4 --ocr-json ocr.json \
        --scene-json scene.json --activity-json activity.json \
        --source-id a --index-id idx1 --output index.json
```

2단계에서는 Index를 바탕으로 `contracts/edit-plan.schema.md`의 형식에 맞춰 `plan.json`을
직접 작성하거나 에이전트에게 작성을 맡깁니다. Index 형식은 `contracts/index.schema.md`에
있습니다. 무음 화면녹화 한 편에 한해서는 `decide_cuts.py`의 제한적인 규칙으로 컷 초안을
만들 수 있습니다. 장면 경계, OCR 글자 수 변화, 추론한 action 시작 뒤까지 이어지는
`screen_activity` 표지가 필요합니다. 위의 활동 관측 단계와 `--activity-json`은
관측된 활동 표지를 추가하지만 필요한 관측이 반드시 생기는 것은 아닙니다. 관측이
없으면 Index를 읽고 계획서를 직접 작성합니다. 이 규칙은 일반 영상에는 맞지 않습니다.

```bash
python3 skills/cut-decision/decide_cuts.py --index index.json --output plan.json
```

```bash
# 3. 계획서 검사 후 렌더링 시간 추정
python3 validate_plan.py --plan plan.json --index index.json
python3 executor/assemble.py --plan plan.json --validate-only
python3 executor/assemble.py --plan plan.json --estimate-only

# 4. 렌더링; 다섯 인수가 모두 필요합니다
python3 executor/assemble.py --plan plan.json --index index.json \
        --source-map source-map.json --out out.mp4 --work work/
```

4단계 전에 각 `source_id`를 실제 파일에 연결하는 `source-map.json`을 만듭니다.
예를 들면 `{"a": "/absolute/path/a.mp4"}`입니다. `--index`, `--source-map`, `--work`는
생략할 수 없습니다. 빠지면 다음 메시지와 함께 렌더링이 중단됩니다.
`--index --source-map --work required unless --estimate-only is used`.
`--work`와 `--out`은 같은 파일시스템에 두고 출력의 상위 디렉터리를 미리 만들어야
합니다. 실행기는 완성 파일을 `os.replace`로 옮깁니다. 다른 볼륨에 전달하려면 완성된
출력을 나중에 복사합니다. 오버레이 이미지·자막 글꼴·음원 자산을 쓰면
`--asset-map asset-map.json`도 전달해야 합니다. 이 JSON 객체는 계획서의
`assets[].asset_id`를 파일 경로에 연결하며, `caption_styles[].font_asset_id`로
참조하는 글꼴도 포함합니다. 예:
`{"mask-1": "/absolute/path/mask.png", "font-1": "/absolute/path/font.ttf"}`.
실행기는 이 파일들의 SHA-256이 선언된 값과 같은지 검사합니다.
모든 도구는 `--help`를 지원합니다.

## 폴더 구조

```text
executor/         EditPlan을 영상으로 렌더링하는 실행기; 계획 도구와 분리되어 있습니다
skills/
  media-index/    장면·정지·움직임·화면 속 글자·음성 관측
  cut-decision/   무음 화면녹화 한 편의 Index에서 컷 초안 생성
  pair-align/     원본과 완성본의 오디오 정렬, 컷 목록 복원
  mask-plan/      민감한 문자열 탐색, 가림 계획, 렌더링 전후 검사
  policy-workflow/ 자막 초안, 배속 적용·검사, 계획서 병합, 컨택트 시트(contact sheet: 여러 영상 프레임을 한 장에 모은 미리보기) 생성
contracts/        Index·EditPlan·도구 출력의 JSON 형식과 검증 규칙
```

## 라이선스

Apache License 2.0이며, 전문은 [LICENSE](LICENSE)에 있습니다. 제3자 구성요소와 ffmpeg
라이선스는 [NOTICE](NOTICE)에 설명되어 있습니다. Draftcut은 ffmpeg를 별도 프로세스로
호출하며 libav 라이브러리를 링크하지 않고 ffmpeg 바이너리도 포함하지 않습니다.
ffmpeg를 Draftcut과 묶어 재배포하지 않아야 합니다. ffmpeg 바이너리를 함께 배포하면
해당 빌드의 라이선스 의무가 적용되며, GPL 빌드라면
GPL 의무도 적용됩니다. 자세한 내용은 NOTICE에 있습니다.
