Trials of Innocence 한국어 패치 v1.0.0 — 스팀덱 시험판 test2

대응 게임: Steam Windows판, App ID 2983140, 빌드 18618782
해당 게임 빌드 업데이트 날짜: 2025-05-27 (한국 시간)
실행 환경: SteamOS / Linux에서 Proton으로 실행하는 Windows판
번역·이미지·폰트는 Windows 정식 v1.0.0과 동일합니다.
스팀덱 실제 기기에서의 게임 실행·화면·조작 검증은 아직 완료하지 않았습니다.
Windows 정식판의 플레이 테스트 완료 기록은 스팀덱 검증을 뜻하지 않습니다.

준비
- 게임을 완전히 종료하고 스팀덱의 데스크톱 모드로 전환합니다.
- 먼저 패치 없는 게임이 Steam에서 정상 실행되는지 확인하세요.
- Python 3.9 이상과 표준 라이브러리가 필요합니다. 추가 Python 패키지는 필요 없습니다.
  Konsole에서 python3 --version으로 확인합니다.
  python3가 없다면 사용 중인 SteamOS 환경에 Python을 준비한 뒤 실행하세요.
- sudo나 관리자 권한은 필요하지 않습니다.
- 설치·복구 중에는 Steam 게임 업데이트나 다른 도구의 게임 파일 수정을 중지하세요.

설치
1. Steam 라이브러리에서 게임의 관리 → 로컬 파일 탐색을 선택합니다.
   Trials of Innocence.exe가 있는 폴더를 사용합니다. 내장 저장소와 SD 카드 모두 가능합니다.
   compatdata 안의 가상 C: 드라이브에는 설치하지 않습니다.
2. 기존 패치가 설치돼 있다면 기존 도구로 먼저 원본을 복구합니다.
   기존 KoreanPatch 폴더는 backup과 함께 다른 이름이나 위치에 보관합니다.
   새 패치 파일을 기존 KoreanPatch 위에 덮어 풀지 마세요.
3. TrialsOfInnocence-KoreanPatch-v1.0.0-SteamDeck-test2.zip을 풀고 KoreanPatch 폴더를 게임 폴더에 넣습니다.
   올바른 배치 예:
     Trials of Innocence/
       Trials of Innocence.exe
       Trials of Innocence_Data/
       KoreanPatch/
         install.sh
         restore.sh
         patch.py
         payload/
4. 파일 관리자에서 KoreanPatch 폴더 안에 터미널(Konsole)을 엽니다.
   다음 명령을 실행합니다. 파일 실행 권한을 따로 바꿀 필요는 없습니다.
     sh ./install.sh
   또는 다음 명령으로 직접 실행할 수 있습니다.
     python3 ./patch.py install
5. 설치 완료 메시지를 확인한 뒤 게이밍 모드로 돌아가 Steam에서 게임을 실행합니다.
   게임 언어에서 한국어를 선택합니다. 일본어 음성 표시는 일본어입니다.
   게임의 기존 Proton 설정을 먼저 사용하세요. 이 패치는 특정 Proton 버전을 요구하지 않습니다.

제거·복구
- 게임을 종료하고 KoreanPatch 폴더에서 다음 명령을 실행합니다.
    sh ./restore.sh
  또는:
    python3 ./patch.py restore
- 추가 파일 72개를 제거하고 교체한 파일 794개를 원본으로 되돌립니다.
- KoreanPatch/backup은 복구 후에도 보관됩니다.
- 한국어 패치가 적용된 상태에서 KoreanPatch 폴더나 backup을 지우지 마세요.
- 설치가 중단됐다는 메시지가 나오면 restore.sh로 복구한 뒤 install.sh를 다시 실행하세요.
- 다른 프로그램이 수정한 파일이나 게임 업데이트가 감지되면 복구를 중단합니다.
  백업을 보존하고 파일을 확인하세요. 변조된 백업으로 강제 복구하지 않습니다.

검증과 참고
- 설치 전 원본 3,875개와 패치 파일의 SHA-256을 확인합니다.
- 대응 빌드가 다르거나 다른 패치가 적용돼 있으면 설치를 중단합니다.
- 일반적인 설치·복구 오류는 완료한 파일 변경을 되돌립니다.
  전원 차단이나 강제 종료 후에는 백업을 유지하고 restore.sh를 실행하세요.
- 게임의 StandaloneWindows64 폴더와 Addressables 카탈로그 경로를 변경하지 않습니다.
  게임 자체는 계속 Windows판으로 Proton에서 실행됩니다.
- 한글 폰트가 게임 리소스에 포함돼 있어 OS에 별도로 폰트를 설치할 필요가 없습니다.
- 대사·증언·선택지·편지·증거물 화면은 실제 스팀덱에서 읽기와 잘림을 확인해야 합니다.
  기본 해상도에서의 가독성과 컨트롤러·트랙패드 조작도 시험 대상입니다.
- 게임 자체의 Proton 실행 오류는 이 리소스 패치의 설치 성공과 별도로 확인해야 합니다.
- Windows에서 만든 동일 v1.0.0의 backup/state.json도 읽을 수 있습니다.
  다른 버전의 백업은 재사용하지 않습니다.
- 글꼴 라이선스와 저작권 고지는 licenses 폴더에 있습니다.
- 시험판 정보는 steamdeck-info.json에 기록돼 있습니다.
