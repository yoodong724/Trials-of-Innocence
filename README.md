# Trials of Innocence 한글패치

**[v1.0.0 한글패치 다운로드](https://github.com/yoodong724/Trials-of-Innocence/releases/tag/v1.0.0)** · [변경 내역](CHANGELOG.md)

## 대응 버전

| 항목 | 버전 |
|---|---|
| 한글패치 | **v1.0.0 (정식 배포)** |
| 게임 | Steam Windows판, App ID 2983140, 빌드 **18618782** |
| 해당 게임 빌드 업데이트 날짜 | **2025년 5월 27일** (한국 시간) |

게임 빌드와 업데이트 날짜: [SteamDB](https://steamdb.info/depot/2983141/).

플레이 테스트를 마친 정식 배포판입니다.
플레이와 수정을 거치며 테스트를 했으나, 배포판 단일 버전으로 처음부터 끝까지 플레이한 테스트는 아니라는 점을 밝힙니다.

## 설치 방법

1. 게임을 종료합니다.
2. 기존 패치가 설치돼 있다면 기존 `KoreanPatch/restore.cmd`로 원본을 복구하고, 기존 `KoreanPatch` 폴더를 **backup 폴더와 함께** 다른 이름이나 위치에 보관합니다.
3. 다운로드 페이지의 **Assets**에서 `TrialsOfInnocence-KoreanPatch-v1.0.0.zip`을 받습니다.
4. Steam 라이브러리에서 게임을 우클릭하고 **관리 → 로컬 파일 탐색**으로 게임 폴더를 엽니다.
5. ZIP 안의 `KoreanPatch` 폴더를 게임 실행 파일이 있는 폴더에 넣습니다.
6. `KoreanPatch/install.cmd`를 실행합니다.
7. 게임을 실행하고 게임 언어에서 **한국어**를 선택합니다. 일본어 음성의 언어명 표시는 **일본어**입니다.

## 패치 업데이트·제거

게임을 종료한 뒤 `KoreanPatch/restore.cmd`를 실행하면 원본으로 복구됩니다.
새 패치를 설치할 때는 복구 후 기존 `KoreanPatch` 폴더와 백업을 보관하고 위 순서로 설치합니다.

## 스팀덱 시험판

[v1.0.0 스팀덱 시험판](https://github.com/yoodong724/Trials-of-Innocence/releases/tag/v1.0.0-steamdeck-test2)은 정식 v1.0.0과 번역·이미지·폰트가 같습니다. SteamOS용 설치·복구 도구를 추가했습니다.
**스팀덱 실기 테스트는 하지 않았습니다.** Linux 임시 게임 사본에서 설치·복구를 검사했으며, 실제 기기의 게임 실행·한글 표시·화면·조작은 미확인입니다.

데스크톱 모드에서 ZIP의 `KoreanPatch`를 게임 폴더에 넣고, 해당 폴더의 Konsole에서 `sh ./install.sh`를 실행합니다. 복구는 `sh ./restore.sh`입니다. Python 3.9 이상이 필요하며 관리자 권한이나 추가 Python 패키지는 필요하지 않습니다.
자세한 설치와 포장 도구 사용법은 [스팀덱 안내](docs/steamdeck.md)를 확인하세요.

## 저장소 소스 안내

게임 대사와 줄거리의 불필요한 노출을 줄이기 위해 번역 본문·수정 이미지·빌드 입력은
저장소에서 제외하고 제작 도구와 설치기 소스만 보관합니다.
**이 저장소의 소스와 원본 게임만으로는 한글패치를 재현할 수 없습니다.**
패치를 사용할 때는 Releases에 첨부한 설치용 ZIP을 받으십시오. GitHub의 `Source code` ZIP은 제작 도구·설치기 소스입니다.
