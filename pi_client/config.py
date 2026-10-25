"""Pi 클라이언트 설정.

값은 환경변수로 넣는다. systemd 서비스는 pi_client/deploy/pi-client.env 파일을 읽는다.
기기 ID와 기기 키는 웹 마이페이지에서 Pi 기기를 추가할 때 한 번만 보여 주는 값이다.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_optional(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass
class ClientSettings:
    # --- 서버 접속 ---
    server_url: str = field(default_factory=lambda: _env("PI_SERVER_URL", "https://192.168.0.10:8443").rstrip("/"))
    device_id: int = field(default_factory=lambda: int(_env("PI_DEVICE_ID", "0")))
    device_key: str = field(default_factory=lambda: _env("PI_DEVICE_KEY", ""))
    # mkcert로 만든 인증서를 쓰면 그 인증 기관(rootCA.pem) 경로. 없으면 시스템 인증서로 확인한다.
    ca_file: Optional[str] = field(default_factory=lambda: _env_optional("PI_CA_FILE"))
    # 서버가 꺼졌을 때 다시 접속하는 간격(초). 마지막 값으로 계속 시도한다.
    reconnect_delays: List[float] = field(default_factory=lambda: [1.0, 2.0, 5.0, 10.0])

    # --- 카메라 ---
    # 'auto' | 'picamera2' | 'opencv' | 'file' | 'fake'. auto는 picamera2(카메라 모듈)를 먼저 시도하고 없으면 opencv(USB 웹캠).
    camera_backend: str = field(default_factory=lambda: _env("PI_CAMERA", "auto"))
    camera_index: int = field(default_factory=lambda: int(_env("PI_CAMERA_INDEX", "0")))
    video_path: Optional[str] = field(default_factory=lambda: _env_optional("PI_VIDEO_PATH"))  # 'file': 시험용 영상
    frame_width: int = 640
    frame_height: int = 480
    fps: float = field(default_factory=lambda: float(_env("PI_FPS", "10")))
    jpeg_quality: int = 70

    # --- 음성 확인 ---
    # 'sounddevice' | 'fake'. 'fake'는 테스트 전용이며 정해 둔 문장을 녹음 대신 보낸다.
    audio_backend: str = field(default_factory=lambda: _env("PI_AUDIO", "sounddevice"))
    prompt_wav: Path = field(default_factory=lambda: Path(_env("PI_PROMPT_WAV", str(BASE_DIR / "assets" / "ask_status.wav"))))
    record_seconds: float = field(default_factory=lambda: float(_env("PI_RECORD_SECONDS", "5")))
    sample_rate: int = 16000
    # 마이크·스피커 장치. 비우면 기본 장치. 'python -m sounddevice'로 목록을 볼 수 있다.
    input_device: Optional[str] = field(default_factory=lambda: _env_optional("PI_INPUT_DEVICE"))
    output_device: Optional[str] = field(default_factory=lambda: _env_optional("PI_OUTPUT_DEVICE"))

    @property
    def ws_url(self) -> str:
        """카메라 WebSocket 주소. 기기 키는 주소가 아니라 X-Device-Key 헤더로 보낸다(로그에 남지 않도록)."""
        base = self.server_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/ws/camera?device_id={self.device_id}"

    def validate(self) -> List[str]:
        """실행할 수 없게 만드는 빠진 설정을 알려 준다."""
        problems = []
        if self.device_id <= 0:
            problems.append("PI_DEVICE_ID가 없습니다. 웹 마이페이지에서 Pi 기기를 추가하고 기기 ID를 넣어 주세요.")
        if not self.device_key:
            problems.append("PI_DEVICE_KEY가 없습니다. 기기를 추가할 때 한 번만 보이는 기기 키를 넣어 주세요.")
        return problems
