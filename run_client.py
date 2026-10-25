"""Pi 클라이언트 실행 파일.

    python run_client.py            # pi_client/deploy/pi-client.env 설정으로 실행
    python run_client.py --check    # 설정과 서버 연결만 확인하고 끝냄
    python run_client.py --fake     # 카메라·마이크 없이 가짜 장치로 실행(시험용)

설정 파일(pi-client.env)의 값을 읽어 환경변수로 넣은 뒤 `python -m pi_client`와 똑같이 실행한다.
이미 지정된 환경변수가 있으면 그 값이 우선한다. Pi에 서비스로 설치했다면(install.sh) 평소에는
서비스가 자동으로 실행하므로, 이 파일은 직접 띄워 보거나 연결을 확인할 때 쓴다.
같은 기기로 두 개를 띄우면 서로 끊으니 먼저 `sudo systemctl stop pi-client`로 서비스를 멈춘다.
"""

import argparse
import importlib.util
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_ENV = ROOT / "pi_client" / "deploy" / "pi-client.env"

REQUIRED = {"websockets": "websockets", "httpx": "httpx", "numpy": "numpy", "cv2": "opencv-python-headless"}


def say(message: str = "") -> None:
    print(message, flush=True)


def load_env_file(path: Path) -> int:
    """KEY=VALUE 줄을 읽어 환경변수로 넣는다. 주석·빈 값은 건너뛰고, 이미 있는 환경변수는 바꾸지 않는다."""
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def check_packages(fake: bool) -> bool:
    missing = [pip for module, pip in REQUIRED.items() if importlib.util.find_spec(module) is None]
    if not fake and importlib.util.find_spec("sounddevice") is None:
        missing.append("sounddevice")
    if missing:
        say("필요한 패키지가 없습니다: " + ", ".join(missing))
        say(f"  {sys.executable} -m pip install -r pi_client/requirements.txt")
        return False
    return True


def check_connection() -> int:
    """서버에 닿는지, 기기 ID·키가 맞는지 확인한다(카메라·마이크는 쓰지 않음)."""
    from pi_client.config import ClientSettings

    settings = ClientSettings()
    key = settings.device_key
    say(f"서버 주소 : {settings.server_url}")
    say(f"기기 ID   : {settings.device_id}")
    say(f"기기 키   : {key[:4] + '…' if key else '(없음)'}")
    say(f"인증 기관 : {settings.ca_file or '(시스템 기본)'}")
    say(f"카메라    : {settings.camera_backend} / 오디오: {settings.audio_backend}")
    problems = settings.validate()
    if problems:
        for p in problems:
            say("! " + p)
        return 2

    url = f"{settings.server_url}/api/devices/{settings.device_id}/state"
    request = urllib.request.Request(url, headers={"X-Device-Key": key})
    context = ssl.create_default_context(cafile=settings.ca_file) if url.startswith("https") else None
    try:
        with urllib.request.urlopen(request, timeout=5, context=context) as res:
            device = json.load(res)["device"]
    except urllib.error.HTTPError as e:
        say(f"! 서버는 응답했지만 거절했습니다 (HTTP {e.code}). 기기 ID·키를 확인하세요"
            + (" (계정이 정지됐을 수도 있습니다)." if e.code == 401 else "."))
        return 3
    except ssl.SSLError as e:
        say(f"! 인증서 확인 실패: {e}. PI_CA_FILE에 서버 PC의 rootCA.pem 경로를 넣으세요.")
        return 1
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        say(f"! 서버에 접속할 수 없습니다: {reason}. 서버가 켜져 있는지, 같은 Wi-Fi인지, 주소가 맞는지 확인하세요.")
        return 1
    say(f"연결 확인 완료: '{device['name']}' (주인: {device.get('owner_name', '?')}), "
        f"지금 {'접속 중' if device['online'] else '접속 안 함'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pi 현장 단말 클라이언트 실행")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help=f"설정 파일 (기본: {DEFAULT_ENV.relative_to(ROOT)})")
    parser.add_argument("--server", help="서버 주소 (PI_SERVER_URL 대신)")
    parser.add_argument("--device-id", help="기기 ID (PI_DEVICE_ID 대신)")
    parser.add_argument("--key", help="기기 키 (PI_DEVICE_KEY 대신)")
    parser.add_argument("--fake", action="store_true", help="가짜 카메라·가짜 오디오로 실행(시험용)")
    parser.add_argument("--check", action="store_true", help="설정과 서버 연결만 확인")
    args = parser.parse_args()

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    for name, value in (("PI_SERVER_URL", args.server), ("PI_DEVICE_ID", args.device_id), ("PI_DEVICE_KEY", args.key)):
        if value:
            os.environ[name] = value
    if args.fake:
        os.environ["PI_CAMERA"] = "fake"
        os.environ["PI_AUDIO"] = "fake"
    if args.env.exists():
        say(f"설정 파일을 읽었습니다: {args.env} ({load_env_file(args.env)}개 값)")
    elif args.env != DEFAULT_ENV:
        say(f"설정 파일이 없습니다: {args.env}")
        return 2
    else:
        say(f"설정 파일이 없어 환경변수만 씁니다. 예시: {DEFAULT_ENV.name}.example 을 복사해 값을 넣으세요.")

    if not check_packages(args.fake or args.check):
        return 1
    if args.check:
        return check_connection()

    from pi_client.__main__ import main as run_client
    return run_client()


if __name__ == "__main__":
    sys.exit(main())
