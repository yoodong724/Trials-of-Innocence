# Trials of Innocence 한글패치

**[v1.0.0 한글패치 다운로드](https://github.com/yoodong724/Trials-of-Innocence/releases/tag/v1.0.0)** · [변경 내역](CHANGELOG.md)

## 대응 버전

| 항목 | 버전 |
|---|---|
| 한글패치 | **v1.0.0 (정식 배포)** |
| 게임 | Steam Windows판, App ID 2983140, 빌드 **18618782** |
| 해당 게임 빌드 업데이트 날짜 | **2025년 5월 27일** (한국 시간) |

게임 빌드와 업데이트 날짜: [SteamDB](https://steamdb.info/depot/2983141/).

플레이 테스트를 마친 정식 배포판입니다. v7.13까지의 모든 번역·그림·설치기 수정이 포함됩니다.
장 선택의 증거물 그림, 5장 후반의 말투·인명, 칸을 벗어나던 선택지와 문자 메시지 설명을 수정했습니다.
저장·불러오기 UI, 단어 단위 줄바꿈, 배경·증거물 그림과 앞선 대사 수정도 모두 포함합니다.
누적 업데이트 내용은 다운로드 페이지에서 접힌 항목을 눌러 확인할 수 있습니다.

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

배포 파일명은 `TrialsOfInnocence-KoreanPatch-v1.0.0.zip`입니다.
후속 수정 배포는 `TrialsOfInnocence-KoreanPatch-v1.0.0-rc2.zip`처럼 버전 뒤에 `-rcN`을 붙입니다.

## 저장소 소스 안내

게임 대사와 줄거리의 불필요한 노출을 줄이기 위해 번역 본문·수정 이미지·빌드 입력은
저장소에서 제외하고 제작 도구와 설치기 소스만 보관합니다.
**이 저장소의 소스와 원본 게임만으로는 한글패치를 재현할 수 없습니다.**
패치를 사용할 때는 Releases에 첨부한 설치용 ZIP을 받으십시오. GitHub의 `Source code` ZIP은 제작 도구·설치기 소스입니다.
