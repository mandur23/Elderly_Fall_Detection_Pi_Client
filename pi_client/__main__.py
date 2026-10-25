"""`python -m pi_client`로 Pi 현장 단말 클라이언트를 실행한다.

설정은 환경변수(PI_*)로 넣는다. systemd 서비스는 pi_client/deploy/pi-client.env를 읽는다.

종료 코드 (pi-client.service의 RestartPreventExitStatus=2 3 4와 맞춘다)
  0  정상 종료(SIGTERM·Ctrl+C)
  1  예상 못 한 오류 -> systemd가 5초 뒤 다시 시작
  2  설정이 잘못됨(PI_DEVICE_ID·PI_DEVICE_KEY 없음, 숫자 형식 오류, 알 수 없는 카메라·오디오 종류 등)
  3  서버가 기기를 거부함(4401 기기 키 오류, 4403 권한 없음)
  4  같은 기기로 다른 클라이언트가 접속해 이 연결이 교체됨(4409)
"""

import asyncio
import logging
import signal
import sys
from typing import List

from pi_client.audio import AudioError, create_audio
from pi_client.camera import CameraError, FrameSource, create_frame_source
from pi_client.client import EXIT_CONFIG, ClientStop, PiClient, RetryingFrameSource
from pi_client.config import ClientSettings

log = logging.getLogger("pi")

CAMERA_BACKENDS = ("auto", "picamera2", "opencv", "file", "fake")
AUDIO_BACKENDS = ("sounddevice", "fake")
# 시작할 때 카메라가 안 열리면 이 간격(초)으로 다시 열어 본다.
CAMERA_RETRY_SECONDS = 5.0


def config_problems(settings: ClientSettings) -> List[str]:
    """ClientSettings.validate()에 더해, 실행해도 소용없는 설정 오류를 모은다."""
    problems = list(settings.validate())
    camera = (settings.camera_backend or "auto").strip().lower()
    if camera not in CAMERA_BACKENDS:
        problems.append(f"PI_CAMERA={settings.camera_backend!r}는 알 수 없는 값입니다 ({', '.join(CAMERA_BACKENDS)} 중 하나).")
    elif camera == "file" and not settings.video_path:
        problems.append("PI_CAMERA=file이면 PI_VIDEO_PATH에 영상 파일 경로를 넣어 주세요.")
    audio = (settings.audio_backend or "sounddevice").strip().lower()
    if audio not in AUDIO_BACKENDS:
        problems.append(f"PI_AUDIO={settings.audio_backend!r}는 알 수 없는 값입니다 ({', '.join(AUDIO_BACKENDS)} 중 하나).")
    if settings.fps <= 0:
        problems.append(f"PI_FPS는 0보다 커야 합니다 (지금 {settings.fps}).")
    if settings.record_seconds <= 0:
        problems.append(f"PI_RECORD_SECONDS는 0보다 커야 합니다 (지금 {settings.record_seconds}).")
    if not settings.server_url.startswith(("http://", "https://")):
        problems.append(f"PI_SERVER_URL은 https:// 또는 http://로 시작해야 합니다 (지금 {settings.server_url!r}).")
    return problems


def open_frame_source(settings: ClientSettings) -> FrameSource:
    """카메라를 연다. 지금 열리지 않아도 끝내지 않고, 연결을 유지한 채 몇 초마다 다시 열어 보게 한다."""
    try:
        return create_frame_source(settings)
    except CameraError as e:
        log.error("%s", e)
        log.error("카메라 없이 서버 연결은 유지합니다(보호자 화면에 '카메라 응답 없음'). %.0f초마다 다시 열어 봅니다.",
                  CAMERA_RETRY_SECONDS)
        return RetryingFrameSource(lambda: create_frame_source(settings), interval=CAMERA_RETRY_SECONDS)


async def _run(client: PiClient) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, client.stop)
        except (NotImplementedError, RuntimeError):  # 신호 처리기를 달 수 없는 환경(Windows, 메인 스레드 아님)
            pass
    await client.run()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = ClientSettings()
    except ValueError as e:
        # int('') 등: pi-client.env에 숫자 항목을 빈 값으로 두면 여기서 난다.
        log.error("설정 값 형식이 잘못되었습니다: %s. pi-client.env의 숫자 항목(PI_DEVICE_ID, PI_CAMERA_INDEX, "
                  "PI_FPS, PI_RECORD_SECONDS)을 확인해 주세요. 기본값을 쓰려면 그 줄을 주석으로 둡니다.", e)
        return EXIT_CONFIG
    problems = config_problems(settings)
    if problems:
        for problem in problems:
            log.error("%s", problem)
        return EXIT_CONFIG
    if settings.server_url.startswith("http://"):
        log.warning("PI_SERVER_URL이 http입니다. 영상과 녹음이 암호화되지 않으니 시험할 때만 쓰고, 시연은 https로 하세요.")

    try:
        audio = create_audio(settings)
    except AudioError as e:
        log.error("%s", e)
        return EXIT_CONFIG
    frame_source = open_frame_source(settings)
    client = PiClient(settings, frame_source, audio)
    log.info("Pi 클라이언트 시작: 서버 %s, 기기 %s, 카메라 %s, 오디오 %s, %.0f fps",
             settings.server_url, settings.device_id, frame_source.name, audio.name, settings.fps)
    try:
        asyncio.run(_run(client))
    except ClientStop as e:
        log.error("%s", e.message)
        return e.code
    except KeyboardInterrupt:  # 신호 처리기를 달기 전에 Ctrl+C
        pass
    finally:
        frame_source.close()
    log.info("Pi 클라이언트 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
