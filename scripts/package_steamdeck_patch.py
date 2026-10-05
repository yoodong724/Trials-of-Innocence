#!/usr/bin/env python3
"""Wrap the approved Windows resource payload with a standalone SteamOS installer."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import secrets
import stat
import zipfile

from game_patch_linux import json_object, validate_manifest
from patch_layout import payload_relative
WINDOWS_EXTRAS = {"patch-manifest.json", "patch-manifest.sha256", "README.ko.txt", "install.cmd",
                  "restore.cmd", "patch.ps1", "licenses/OFL-1.1.txt", "licenses/FONT-NOTICE.txt"}
from toi_common import (ToiError, load_json, sha256_bytes, sha256_file,
                        staged_output_directory, write_bytes_new)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "patch/steamdeck-package-v1.0.0.json"
SCRIPTS = {"patch.py": "installer/linux/patch.py", "install.sh": "installer/linux/install.sh",
           "restore.sh": "installer/linux/restore.sh"}
INHERITED = {"patch-manifest.json", "patch-manifest.sha256", "licenses/OFL-1.1.txt",
             "licenses/FONT-NOTICE.txt"}


def readme(config, manifest):
    replaced = sum(r["operation"] == "replace" for r in manifest["changes"])
    added = len(manifest["changes"]) - replaced
    return f"""Trials of Innocence 한국어 패치 {config['version']} — 스팀덱 시험판 {config['revision']}

대응 게임: Steam Windows판, App ID {config['steam_appid']}, 빌드 {config['steam_build']}
해당 게임 빌드 업데이트 날짜: {config['steam_build_update_date_kst']} (한국 시간)
실행 환경: SteamOS / Linux에서 Proton으로 실행하는 Windows판
번역·이미지·폰트는 Windows 정식 {config['version']}과 동일합니다.
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
3. {config['archive_stem']}.zip을 풀고 KoreanPatch 폴더를 게임 폴더에 넣습니다.
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
- 추가 파일 {added}개를 제거하고 교체한 파일 {replaced}개를 원본으로 되돌립니다.
- KoreanPatch/backup은 복구 후에도 보관됩니다.
- 한국어 패치가 적용된 상태에서 KoreanPatch 폴더나 backup을 지우지 마세요.
- 설치가 중단됐다는 메시지가 나오면 restore.sh로 복구한 뒤 install.sh를 다시 실행하세요.
- 다른 프로그램이 수정한 파일이나 게임 업데이트가 감지되면 복구를 중단합니다.
  백업을 보존하고 파일을 확인하세요. 변조된 백업으로 강제 복구하지 않습니다.

검증과 참고
- 설치 전 원본 {len(manifest['game_files']):,}개와 패치 파일의 SHA-256을 확인합니다.
- 대응 빌드가 다르거나 다른 패치가 적용돼 있으면 설치를 중단합니다.
- 일반적인 설치·복구 오류는 완료한 파일 변경을 되돌립니다.
  전원 차단이나 강제 종료 후에는 백업을 유지하고 restore.sh를 실행하세요.
- 게임의 StandaloneWindows64 폴더와 Addressables 카탈로그 경로를 변경하지 않습니다.
  게임 자체는 계속 Windows판으로 Proton에서 실행됩니다.
- 한글 폰트가 게임 리소스에 포함돼 있어 OS에 별도로 폰트를 설치할 필요가 없습니다.
- 대사·증언·선택지·편지·증거물 화면은 실제 스팀덱에서 읽기와 잘림을 확인해야 합니다.
  기본 해상도에서의 가독성과 컨트롤러·트랙패드 조작도 시험 대상입니다.
- 게임 자체의 Proton 실행 오류는 이 리소스 패치의 설치 성공과 별도로 확인해야 합니다.
- Windows에서 만든 동일 {config['version']}의 backup/state.json도 읽을 수 있습니다.
  다른 버전의 백업은 재사용하지 않습니다.
- 글꼴 라이선스와 저작권 고지는 licenses 폴더에 있습니다.
- 시험판 정보는 steamdeck-info.json에 기록돼 있습니다.
"""


def package(config_path=CONFIG, release_dir=ROOT / "release", source_archive=None):
    config = load_json(config_path)
    stem = config["archive_stem"]
    if (config.get("format") != "toi-l10n/steamdeck-package-v1" or
            not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc[1-9]\d*)?", config["version"]) or
            not re.fullmatch(r"test[1-9]\d*", config["revision"]) or
            stem != f"TrialsOfInnocence-KoreanPatch-{config['version']}-SteamDeck-{config['revision']}"):
        raise ToiError("SteamDeck 시험 배포 이름 또는 형식이 다름")
    source_path = Path(source_archive) if source_archive is not None else ROOT / config["source_archive"]
    if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_size != config["source_size"]:
        raise ToiError("기준 Windows ZIP 크기가 다름")
    expected_sidecar = config["source_zip_sha256"] + "  " + source_path.name
    if source_path.with_name(source_path.name + ".sha256").read_text().strip() != expected_sidecar:
        raise ToiError("기준 Windows ZIP 체크섬 기록이 다름")
    output, archive = release_dir / stem, release_dir / (stem + ".zip")
    sidecar = archive.with_name(archive.name + ".sha256")
    temporary = archive.with_name(archive.name + ".tmp-" + secrets.token_hex(16))
    if any(p.exists() or p.is_symlink() for p in (output, archive, sidecar, temporary)):
        raise ToiError("SteamDeck 배포 출력이 이미 존재함")
    with zipfile.ZipFile(source_path) as source:
        raw = source.read("KoreanPatch/patch-manifest.json")
        manifest = json_object(raw)
        validate_manifest(manifest)
        if manifest["patch_id"] != config["source_patch_id"] or manifest.get("release_version") != config["version"]:
            raise ToiError("기준 Windows 패치 ID 또는 버전이 다름")
        if source.read("KoreanPatch/patch-manifest.sha256").decode().strip() != sha256_bytes(raw):
            raise ToiError("기준 manifest 체크섬이 다름")
        payload = {"payload/" + payload_relative(row) for row in manifest["changes"]}
        source_names = {"KoreanPatch/" + name for name in WINDOWS_EXTRAS | payload}
        if len(source.namelist()) != len(source_names) or set(source.namelist()) != source_names:
            raise ToiError("기준 Windows ZIP 구성에 중복 또는 여분 파일이 있음")
        for entry in source.infolist():
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFREG):
                raise ToiError("기준 ZIP에 링크 또는 특수 파일이 있음")
        for row in manifest["changes"]:
            if source.getinfo("KoreanPatch/payload/" + row["payload_path"]).file_size != row["output_size"]:
                raise ToiError("기준 payload 크기가 다름")
        scripts = {name: (ROOT / path).read_bytes() for name, path in SCRIPTS.items()}
        info = {"format": "toi-l10n/steamdeck-distribution-v1", "version": config["version"],
                "revision": config["revision"], "platform": "SteamOS/Linux + Proton (Windows game)",
                "steam_appid": config["steam_appid"], "steam_build": config["steam_build"],
                "source_zip_sha256": config["source_zip_sha256"], "patch_id": manifest["patch_id"],
                "payload_changed": False, "steamdeck_runtime_test": "pending",
                "installer_sha256": {name: sha256_bytes(data) for name, data in scripts.items()}}
        additions = scripts | {"README.ko.txt": readme(config, manifest).encode("utf-8-sig"),
                               "steamdeck-info.json": (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode()}
        inherited = {"KoreanPatch/" + name for name in INHERITED | payload}
        payload_hashes = {"KoreanPatch/payload/" + row["payload_path"]: row["output_sha256"]
                          for row in manifest["changes"]}
        expected_names = inherited | {"KoreanPatch/" + name for name in additions}
        expected = {}
        owned = False
        release_dir.mkdir(parents=True, exist_ok=True)
        try:
            with staged_output_directory(output) as staging:
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as target:
                    owned = True
                    for entry in source.infolist():
                        if entry.filename not in inherited:
                            continue
                        data = source.read(entry.filename)  # Check CRC while preserving every inherited byte.
                        if entry.filename in payload_hashes and sha256_bytes(data) != payload_hashes[entry.filename]:
                            raise ToiError("기준 Windows payload SHA-256 불일치: " + entry.filename)
                        target.writestr(copy.copy(entry), data)
                        write_bytes_new(staging / entry.filename, data)
                        expected[entry.filename] = (entry.file_size, entry.CRC)
                    for name, data in sorted(additions.items()):
                        filename = "KoreanPatch/" + name
                        entry = zipfile.ZipInfo(filename, date_time=(2026, 10, 5, 0, 0, 0))
                        entry.create_system = 3
                        mode = 0o755 if name in ("install.sh", "restore.sh") else 0o644
                        entry.external_attr = (stat.S_IFREG | mode) << 16
                        target.writestr(entry, data)
                        write_bytes_new(staging / filename, data)
                        os.chmod(staging / filename, mode)
                        saved = target.getinfo(filename)
                        expected[filename] = (saved.file_size, saved.CRC)
                with zipfile.ZipFile(temporary) as check:
                    if set(check.namelist()) != expected_names or len(check.namelist()) != len(expected_names):
                        raise ToiError("SteamDeck ZIP 파일 구성이 다름")
                    if any((e.file_size, e.CRC) != expected[e.filename] for e in check.infolist()):
                        raise ToiError("SteamDeck ZIP 크기 또는 CRC가 다름")
                    if check.testzip() is not None:
                        raise ToiError("SteamDeck ZIP CRC 확인 실패")
                    if check.read("KoreanPatch/patch-manifest.json") != raw:
                        raise ToiError("SteamDeck manifest 바이트가 다름")
            os.link(temporary, archive)
            temporary.unlink()
        except BaseException:
            if owned:
                temporary.unlink(missing_ok=True)
            raise
    checksum = sha256_file(archive)
    write_bytes_new(sidecar, (checksum + "  " + archive.name + "\n").encode())
    return {"zip": str(archive), "zip_size": archive.stat().st_size, "zip_sha256": checksum,
            "patch_id": manifest["patch_id"], "payload_files": len(payload),
            "replaced_files": sum(r["operation"] == "replace" for r in manifest["changes"]),
            "added_files": sum(r["operation"] == "add" for r in manifest["changes"]),
            "entries": len(expected_names), "steamdeck_runtime_test": "pending",
            "source_archive_SHA256_passes": 0, "archive_SHA256_passes": 1, "archive_CRC_passes": 1}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--source-zip", type=Path, help="Downloaded v1.0.0 Windows installation ZIP")
    parser.add_argument("--release-dir", type=Path, default=ROOT / "release")
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.config, args.release_dir, args.source_zip), ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, f"SteamDeck packaging: {exc}\n")
