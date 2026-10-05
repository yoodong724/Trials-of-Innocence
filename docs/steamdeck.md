# 스팀덱 시험판

정식 v1.0.0과 동일한 번역·이미지·폰트를 사용하는 SteamOS용 시험 배포판입니다.
게임은 Steam Windows판 App ID 2983140, 빌드 18618782를 Proton으로 실행합니다.
게임 빌드 업데이트 날짜는 2025년 5월 27일 한국 시간입니다.

**스팀덱 실기 테스트는 하지 않았습니다.** Linux 임시 게임 사본에서 설치·복구를 확인했으며,
실제 기기의 게임 실행, 한글 표시, 화면 가독성, 컨트롤러·트랙패드 조작은 미확인입니다.
Windows판의 기존 플레이 테스트 기록은 스팀덱 실행 검증을 뜻하지 않습니다.

## 설치와 복구

1. 게임을 종료하고 데스크톱 모드로 전환합니다.
2. 기존 패치는 기존 도구로 먼저 복구하고 KoreanPatch와 backup을 함께 보관합니다.
3. Releases에서 `TrialsOfInnocence-KoreanPatch-v1.0.0-SteamDeck-test2.zip`을 받습니다.
4. ZIP의 KoreanPatch를 Trials of Innocence.exe와 같은 게임 폴더에 넣습니다.
   Steam의 관리 → 로컬 파일 탐색으로 게임 폴더를 열 수 있습니다.
5. KoreanPatch 폴더에서 Konsole을 열고 `sh ./install.sh`를 실행합니다.
6. 설치 완료 후 Steam에서 게임을 실행하고 게임 언어에서 한국어를 선택합니다.

복구는 게임을 종료하고 같은 폴더에서 `sh ./restore.sh`를 실행합니다.
직접 실행은 `python3 ./patch.py install` 또는 `python3 ./patch.py restore`입니다.
Python 3.9 이상과 표준 라이브러리를 사용하며 추가 Python 패키지와 sudo는 필요하지 않습니다.
내장 저장소와 SD 카드 모두 사용할 수 있습니다. compatdata 안에 설치하지 않습니다.
게임의 StandaloneWindows64 이름과 Addressables 경로를 변경하지 않습니다.

원본·패치·백업의 SHA-256이 맞지 않으면 중단하며, 외부 수정 파일을 강제로 덮어쓰지 않습니다.
설치나 복구 중에는 Steam 게임 업데이트와 다른 파일 수정 도구를 중지하세요.
완료 전 진행 상태를 기록하고 일반 오류는 완료한 변경을 되돌립니다.
강제 종료 후에는 backup을 보존하고 restore를 먼저 실행합니다.

## 포장 도구

`installer/linux/`는 설치기 소스이며 `scripts/game_patch_linux.py`와 patch.py의 바이트를
검사에서 대조합니다. 게임 리소스나 번역 입력은 소스에 포함하지 않습니다.

이미 배포한 Windows v1.0.0 ZIP과 그 SHA-256 파일을 같은 디렉터리에 내려받으면
다음 명령으로 SteamOS 설치기가 포함된 ZIP을 만들 수 있습니다.

```sh
python scripts/package_steamdeck_patch.py \
  --source-zip /path/to/TrialsOfInnocence-KoreanPatch-v1.0.0.zip \
  --release-dir /path/to/new-output
```

기준은 `patch/steamdeck-package-v1.0.0.json`에 고정합니다. 기준 ZIP의 크기·체크섬 기록,
manifest의 자체 ID, 정확한 ZIP 항목 집합을 확인합니다. 상속 파일을 읽는 동안 CRC를
검사하고, 각 payload의 SHA-256을 manifest와 대조합니다. 번역 데이터를 재빌드하거나
추가하지 않습니다. 기준 ZIP의 전체 해시를 반복해서 계산하지 않습니다.
새 ZIP의 CRC와 SHA-256을 각각 한 번 확인하며 기존 출력은 덮어쓰지 않습니다.

```sh
python scripts/verify.py
python -m unittest discover -s tests
```

설치용 ZIP에는 patch.py와 install.sh·restore.sh가 있으며 Windows cmd·PowerShell 도구는
들어 있지 않습니다. manifest와 payload는 Windows v1.0.0과 동일합니다.
steamdeck-info.json과 설치 안내에 실기 검증 상태를 pending으로 표시합니다.
GitHub Source code ZIP은 설치용 패치가 아닙니다.
