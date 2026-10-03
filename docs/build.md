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
  --output build/v7.8 --zip release/Trials-of-Innocence-KoreanPatch-test-20261004-v7.8.zip
python scripts/verify.py --inputs /path/to/private-inputs --folder build/v7.8/KoreanPatch \
  --zip release/Trials-of-Innocence-KoreanPatch-test-20261004-v7.8.zip
```

`--inputs`에는 기존 source-lock, manifest, 번역 JSON, 이미지 입력, 변경 데이터와 해당
lock에 연결된 기준 파일을 갖춘 디렉터리를 지정합니다. 개인 경로는 Git에 기록하지 않습니다.
출력은 게임·도구·제작 입력과 분리한 새 경로여야 합니다.
번역·증거물·이미지를 바꾼 개발 빌드는 `--edited`를 추가합니다.

`verify.py`를 입력 없이 실행하면 배포 소스에 제작 본문이 없는지 검사합니다.
이 검사는 패치 재빌드 검증과 구분되며 CI에서도 게임 없이 실행합니다.
입력이 있는 재빌드에서는 원본 3,875개와 패치 862개·ZIP 870개 항목을 확인합니다.

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
