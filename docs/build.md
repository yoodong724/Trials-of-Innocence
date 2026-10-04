# 도구 사용과 로컬 제작 입력

저장소에는 제작 도구·설치기·OFL 폰트 소스만 있습니다. 대사 본문과 게임 콘텐츠의
불필요한 노출을 줄이기 위해 번역, 말투 자료, 이미지 변경 영역, 리소스 차이 데이터는
로컬 입력으로 분리했습니다. 저장소 소스와 원본 게임만으로 패치를 재현할 수 없습니다.

패치를 사용할 때는 Releases의 ZIP을 설치합니다. 아래 명령은 별도로 보관한 제작 입력을
가진 제작자용입니다. 제작 입력이 없으면 빌드 도구가 명확한 오류로 종료합니다.

```bash
python -m pip install -r requirements.txt
python scripts/verify.py
python -m unittest discover -s tests
python scripts/build.py --inputs /path/to/private-inputs --game "/path/to/Trials of Innocence" \
  --output build/v1.0.0 --zip release/TrialsOfInnocence-KoreanPatch-v1.0.0.zip
python scripts/verify.py --inputs /path/to/private-inputs --folder build/v1.0.0/KoreanPatch \
  --zip release/TrialsOfInnocence-KoreanPatch-v1.0.0.zip
```

`--inputs`에는 기존 source-lock, manifest, 번역 JSON, 이미지 입력, 변경 데이터와 해당
lock에 연결된 기준 파일을 갖춘 디렉터리를 지정합니다. 개인 경로는 Git에 기록하지 않습니다.
출력은 게임·도구·제작 입력과 분리한 새 경로여야 합니다.
번역·증거물·이미지를 바꾼 개발 빌드는 `--edited`를 추가합니다.

`verify.py`를 입력 없이 실행하면 배포 소스에 제작 본문이 없는지 검사합니다.
이 검사는 패치 재빌드 검증과 구분되며 CI에서도 게임 없이 실행합니다.
입력이 있는 재빌드에서는 원본 3,875개와 패치 866개·ZIP 874개 항목을 확인합니다.

v7.1부터 payload와 새 백업은 게임 상대 경로의 SHA-256을 이름으로 사용하는
한 단계의 ASCII 파일로 보관합니다. 실제 게임 경로는 manifest의 `path`, 패치 내부
파일명은 `payload_path`로 구분합니다. 기존 제작 입력은 빌드 때 새 배포 구조로 변환합니다.
설치기는 실제 게임 폴더에서 원본·패치·백업·임시 파일 경로를 변경 전에 검사합니다.
전체 경로 검사는 `python scripts/audit_patch_paths.py --manifest <patch-manifest.json>`으로
실행하며, 다른 설치 위치는 `--game-root`로 지정합니다.

Windows PowerShell 설치·복구 회귀 테스트는 WSL에서
`TOI_WINDOWS_PATCH_TESTS=1 python -m unittest discover -s tests`로 실행합니다.
게임·제작 입력이 필요 없는 합성 fixture와 Windows 임시 폴더를 사용하며,
시험 프로세스 안에서만 긴 경로 제한을 활성화합니다.

폰트 재생성은 `prepare_fonts.py --inputs /path/to/private-inputs --game ... --output ...`를
사용합니다. 원본 OFL 폰트와 과거 BASE 테이블은 저장소에 있으며 고지를 유지합니다.

## 정식 배포의 파일명과 ZIP 재현

정식 배포 파일명은 `TrialsOfInnocence-KoreanPatch-v1.0.0.zip`입니다.
후속 수정 배포는 `TrialsOfInnocence-KoreanPatch-v1.0.0-rc2.zip`처럼 버전 뒤에 `-rcN`을 붙입니다.

`patch/build-reference.json`의 `zip_format`은 해당 Release의 ZIP 항목 순서와 고정 시간을 기록합니다.
v1.0.0은 manifest의 changes 순서대로 payload를 넣은 뒤, 설치기·안내·라이선스 등 나머지 파일을
이름순으로 넣습니다. 이 정보가 없는 과거 참조는 기존 이름순과 시간을 사용합니다.
제작 입력의 manifest·payload뿐 아니라 배포 안내문·설치기·라이선스와 ZIP 메타데이터까지
같아야 `verify.py --inputs ... --folder ... --zip ...`의 기준 ZIP 지문 검사를 통과합니다.

사용자의 완료된 플레이 테스트를 기준으로 v7.13의 payload·설치기를 그대로 승계해 v1.0.0으로 배포했습니다.
승격 시 바뀐 ZIP 항목은 버전 manifest·manifest 체크섬·설치 안내 세 개이며, 원본 게임과 payload의
전체 SHA-256 검사를 반복하지 않았습니다. 최종 배포 ZIP의 SHA-256은 한 번 계산했습니다.
설치·복구 시 원본·payload·백업을 검사하는 기존 보호 동작은 유지합니다.
