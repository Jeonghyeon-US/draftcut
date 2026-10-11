# Draftcut — AI 코딩 에이전트 지침

[English](AGENTS.md) · 한국어

이 파일은 AI 코딩 에이전트 지침의 한국어판입니다. `CLAUDE.md`는 영문판인
`AGENTS.md`로 연결됩니다.
사용자가 "이 저장소를 설치해서 내 영상을 편집해 줘"라고 요청하면 아래 순서를 따르십시오.

## 0. 이 저장소가 하는 일과 지원하지 않는 일

| 단계 | 내용 | 위치 |
|---|---|---|
| 1. 관측 | 영상에서 장면 경계·화면 속 글자·발화·움직임을 추출해 Index(관측 목록)를 만듭니다 | `skills/media-index/` |
| 2. 계획 | `contracts/`의 형식과 검증 규칙에 따라 EditPlan(편집 계획서)을 작성합니다 | `contracts/` |
| 3. 실행 | EditPlan을 ffmpeg로 실행해 영상 파일을 만듭니다 | `executor/assemble.py` |

어디를 자를지는 에이전트가 판단합니다. 저장소는 관측 정보, 계획서 형식, 실행기를
제공합니다. `skills/cut-decision/decide_cuts.py`는 무음 화면녹화 한 편을 위한 제한적인
규칙을 사용하며 일반 영상에는 맞지 않습니다. 일반 영상은 Index를 읽고 EditPlan을
직접 작성하십시오.

출력은 영상 파일입니다. Premiere·Final Cut Pro·Resolve로 내보내는 기능은 없습니다.
사용자가 비선형 편집 프로그램(NLE)에서 이어서 편집하길 원하면 현재 지원하지
않는다고 설명하십시오.

## 1. 환경을 점검하고 설치 전에 허가를 받으십시오

```bash
python3 check_env.py --json
```

`ready`가 `false`이면 `install_commands`에 설치 명령 제안이 들어 있습니다.
이 점검기는 필수 모듈과 실행 파일의 존재를 확인하지만, 패키지 버전·인코더·필터
지원이나 계획서의 렌더링 가능 여부는 검사하지 않습니다.
무엇을 왜 설치할지 설명하고, 설치 명령을 실행하기 전에 사용자의 허가를 받으십시오.

| 필수 | 없을 때의 제한 |
|---|---|
| Python 3.10+ · numpy · Pillow | 스크립트 실행에는 Python 3.10+가 필요합니다. numpy는 신호·오디오 분석에, Pillow는 자막 이미지에 쓰입니다. 전체 편집 흐름에는 두 패키지가 필요하며, 환경 점검과 일부 JSON 검사·기록 도구는 표준 라이브러리만 사용합니다 |
| `ffmpeg` · `ffprobe` | 렌더링할 수 없습니다. `DRAFTCUT_FFMPEG`·`DRAFTCUT_FFPROBE`는 환경 점검기·실행기·`skills/mask-plan/verify_render.py`에서 읽으며, 관측·정렬 도구에는 PATH의 `ffmpeg`·`ffprobe`가 필요합니다 |

| 선택 | 없을 때의 제한 |
|---|---|
| pyobjc (macOS) | OCR과 아래 Index 생성 흐름을 사용할 수 없습니다. 실행기에는 기존의 유효한 Index와 EditPlan을 제공할 수 있습니다 |
| mlx-whisper (Apple Silicon) | 음성 전사만 사용할 수 없습니다. 전사본을 직접 제공할 수 있습니다 |

## 2. 작업 순서

### 1. 영상을 관측하고 Index를 만드십시오

이 흐름에는 OCR을 위한 macOS와 PyObjC가 필요합니다. `build_index.py`는
`--ocr-json`을 필수로 받으며 이 빌더는 OCR 없이 Index를 만드는 경로를 제공하지 않습니다.
다른 플랫폼에서는 해당 플랫폼의 패키지 관리자로 ffmpeg/ffprobe를 설치하고,
기존의 유효한 Index와 EditPlan을 실행기에 제공하십시오.

```bash
python3 skills/media-index/scene_detect.py --input IN.mp4 --output scene.json
python3 skills/media-index/ocr_vision.py   --input IN.mp4 --output ocr.json --frames-dir frames/
python3 skills/media-index/activity_mad.py --input IN.mp4 --output activity.json
python3 skills/media-index/build_index.py  --input IN.mp4 --ocr-json ocr.json \
        --scene-json scene.json --activity-json activity.json \
        --source-id a --index-id idx1 --output index.json
```

모든 도구는 `--help`를 지원합니다. `skills/media-index/`에는 화면 활동·정지 구간
검출·음성 전사 도구도 있습니다. `decide_cuts.py`의 초안 규칙에는 장면 경계,
OCR 글자 수 변화, 추론한 action 시작 뒤까지 이어지는 `screen_activity` 표지가
필요합니다. 위의 활동 관측 단계와 `--activity-json`은 관측된 활동 표지를
추가하지만 필요한 관측이 반드시 생기는 것은 아닙니다. 관측이 없으면 Index를 읽고
계획서를 직접 작성하십시오.

### 2. EditPlan을 작성하십시오

- 형식: `contracts/edit-plan.schema.json`(기계 판독용)과 `contracts/edit-plan.schema.md`(설명).
- 예시: `contracts/examples/edit-plan.valid.json`. 승인 상태와 시간 계산에 쓰인 값은
  설명을 위한 예시입니다. 실제 계획서에 그대로 복사할 수 있는 검증된 값이 아닙니다.

다음 네 가지 오류에 주의하십시오.

1. 시간은 정수 틱 값 `value`와 시간 단위 `timebase`로 기록합니다.
   초 단위 값은 `value × timebase.num / timebase.den`인 유리수(정수의 비)입니다. 예:
   `{"value": 153600, "timebase": {"num": 1, "den": 15360}}`. 소수점 초를 쓰지
   마십시오. 반올림으로 프레임이 어긋날 수 있습니다.
2. 구간은 반열림입니다. `start`는 포함하고 `end`는 제외합니다.
3. 재배열을 포함한 모든 `action`에는 `source_segment_id`가 필요합니다.
4. `evidence_ids`가 가리키는 증거의 시간 구간은 동작의 원본 세그먼트 구간과
   겹쳐야 합니다.

### 3. 렌더링 전에 아래 순서로 검사하십시오

```bash
python3 validate_plan.py --plan plan.json --index index.json     # 형식과 구조 규칙
python3 executor/assemble.py --plan plan.json --validate-only    # 실행기 자체 검사
python3 executor/assemble.py --plan plan.json --estimate-only    # 예상 렌더링 시간
```

두 검사를 모두 실행한 뒤 시간을 추정하십시오. `validate_plan.py`는 `contracts/`의
JSON Schema(형식 정의)와 구조 규칙을 검사하고 오류가 있는 필드를 정확히 알려 줍니다.

```text
CONTRACT FAIL: edit-plan schema: /actions/2: required property 'source_segment_id' is missing
```

**`--validate-only`의 `validated` 결과만으로 계획서가 올바르다고 판단할 수 없습니다.**
이 옵션은 실행기의 계획서 내부 검사만 수행하며 스키마를 읽지 않습니다. 형식이나
검증 규칙을 어긴 계획서도 `validated` 결과를 받고 렌더링될 수 있으며, 결과는
잘못될 수 있습니다. `validate_plan.py`를 먼저 실행하십시오. `--validate-only`는
자산 파일·해시를 읽거나 ffmpeg를 실행하지도 않습니다. 출력의 `not_checked`를
확인하십시오.

### 4. 필수 인수 다섯 개를 모두 전달해 렌더링하십시오

```bash
python3 executor/assemble.py --plan plan.json --index index.json \
        --source-map source-map.json --out out.mp4 --work work/
```

`source-map.json`은 계획서의 각 `source_id`를 실제 파일에 연결합니다.

```json
{"a": "/absolute/path/IN.mp4"}
```

`--work`와 `--out`은 같은 파일시스템에 두고 출력의 상위 디렉터리를 미리
만드십시오. 실행기는 완성 파일을 `os.replace`로 옮깁니다. 다른 볼륨에 전달하려면
완성된 출력을 나중에 복사하십시오. 오버레이 이미지·자막 글꼴·음원 자산을 쓰면
`--asset-map asset-map.json`도 전달하십시오. 이 JSON 객체는 계획서의
`assets[].asset_id`를 파일 경로에 연결하며, `caption_styles[].font_asset_id`로
참조하는 글꼴도 포함합니다. 예:

```json
{"mask-1": "/absolute/path/mask.png", "font-1": "/absolute/path/font.ttf"}
```

실행기는 실제 파일의 SHA-256이 선언된 값과 같은지 검사합니다.
성공하면 실행기가 `{"status": "assembled", ...}`를 반환합니다.

## 3. 실패했을 때

| 증상 | 의미 |
|---|---|
| `--index --source-map --work required unless --estimate-only is used` | 4단계의 필수 인수 다섯 개를 모두 전달하지 않았습니다 |
| `CONTRACT FAIL: ...` | `validate_plan.py`가 오류가 있는 필드와 규칙을 정확히 알려 줍니다 |
| `{"status": "rejected", ...}` | `--validate-only` 거부 결과에는 `errors`가 있고, 추정·렌더 실패에는 `error`가 있습니다. 메시지와, 있으면 `--work`의 `assemble-evidence.json`을 읽으십시오 |
| 렌더링 중간에 중단됩니다 | 미디어·ffmpeg·호환성·프레임 수·오디오·자산 문제로 중단될 수 있습니다. 보고된 단계와 `--work`의 증거를 확인하십시오 |

추측하지 말고 구체적인 거부 메시지를 읽어 계획서를 수정하십시오.
검사를 우회하지 마십시오. 형식이나 검증 규칙을 어긴 계획서는 렌더링될 수 있어도
그 결과를 신뢰할 수 없습니다.

## 4. 사용자에게 설명해야 할 한계

- 어디를 자를지는 에이전트의 판단이며, 저장소가 보장하지 않습니다.
- 출력은 영상 파일이며 NLE 프로젝트로 내보낼 수 없습니다.
- 색보정·룩업 테이블(LUT)·멀티캠·그래픽 애니메이션·자막 텍스트 교체·더빙은
  지원하지 않습니다.
- 실행기의 `actions[]`에는 동작 이름 25개를 넣을 수 있습니다.
  `contracts/action-executability.json`과 실행기의 지원 범위를 함께 확인하십시오.
  레지스트리의 `yes(effect-verified)`는 효과 검증 표지이며 계획서에서 렌더링까지
  이어지는 경로를 보장하지 않습니다. J컷·L컷에도 이 값이 있지만 실행기의 렌더
  경로는 없습니다. 개발에 쓴 입력 자료와 기록은 이 저장소에 포함되지 않습니다.
  렌더링 전에 `validate_plan.py`와 `executor/assemble.py --validate-only`를
  둘 다 실행하십시오.
- `act_export_media`·`act_change_canvas`·`act_extract_capture_evidence_frames`·
  `act_remux_media`는 `actions[]`에서 거부됩니다. 내보내기는 실행기의 기본 출력,
  캔버스 설정은 `output_policy`, 프레임 추출은 별도 관측 도구가 처리합니다.
  독립적인 리먹스 렌더 경로는 없습니다.
