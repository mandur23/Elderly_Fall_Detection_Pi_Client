"""카메라 입력. 어떤 카메라든 '보낼 JPEG 바이트 한 장'을 꺼내 주는 FrameSource로 감싼다.

- Camera: 실제 장치에서 BGR 프레임(np.ndarray)을 읽는다. Picamera2(카메라 모듈), OpenCV(USB 웹캠), 영상 파일.
- FrameSource: Camera + JPEG 인코딩. 클라이언트는 next_jpeg()만 부른다.
  가짜 백엔드(FakeFrameSource)는 이미 만들어 둔 바이트를 그대로 내보낸다.
  서버의 가짜 자세 추정기는 프레임 바이트를 JSON으로 읽기 때문이다(server/detection/pose.py fake_frame).

read()/next_jpeg()는 블로킹 함수이므로 이벤트 루프에서는 asyncio.to_thread로 호출한다.
picamera2·cv2 장치 객체는 스레드 하나에서 차례로 쓰는 것을 전제로 한다(동시에 두 번 부르지 않는다).
"""

import json
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

import cv2
import numpy as np

log = logging.getLogger("pi.camera")


class CameraError(RuntimeError):
    """카메라를 열 수 없거나 설정이 잘못됨. 메시지는 사용자에게 그대로 보여 줄 수 있는 한국어."""


def encode_jpeg(frame: np.ndarray, quality: int, width: Optional[int] = None, height: Optional[int] = None) -> bytes:
    """BGR 프레임을 JPEG 바이트로 만든다. width/height를 주면 크기가 다를 때만 줄이거나 늘린다.

    서버는 정해진 크기를 기준으로 속도·위치를 계산하므로 카메라가 다른 크기를 주더라도 맞춰서 보낸다.
    """
    if frame is None or frame.size == 0:
        raise CameraError("빈 프레임은 인코딩할 수 없습니다")
    if width and height and (frame.shape[1] != width or frame.shape[0] != height):
        # 줄일 때는 INTER_AREA가 깨짐(모아레)이 적다.
        frame = cv2.resize(frame, (int(width), int(height)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise CameraError("JPEG 인코딩 실패")
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Camera: 실제 장치
# ---------------------------------------------------------------------------

class Camera:
    """프레임을 읽는 장치. open()은 실패하면 CameraError, read()는 실패하면 None."""

    name = "base"

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> Optional[np.ndarray]:
        """BGR 프레임 한 장. 읽기 실패면 None (예외를 던지지 않는다)."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Picamera2Camera(Camera):
    """라즈베리파이 카메라 모듈(CSI). picamera2는 apt(python3-picamera2)로만 설치되므로 쓸 때만 불러온다."""

    name = "picamera2"

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self._cam = None

    def open(self) -> None:
        if self._cam is not None:
            return
        try:
            from picamera2 import Picamera2
        except ImportError as e:
            raise CameraError(
                "picamera2를 불러올 수 없습니다. 'sudo apt install python3-picamera2'로 설치하고 "
                "가상환경을 --system-site-packages로 만들었는지 확인해 주세요."
            ) from e
        except Exception as e:
            # apt의 picamera2(simplejpeg 등)와 가상환경의 numpy가 맞지 않으면 ImportError가 아니라
            # 'ValueError: numpy.dtype size changed' 같은 예외가 난다. auto에서 USB 웹캠으로 넘어가도록 CameraError로 바꾼다.
            raise CameraError(
                f"picamera2를 불러오다 오류가 났습니다({e!r}). 가상환경의 numpy를 apt 버전과 맞춰 주세요"
                "(pi_client/requirements.txt 설명 참고)."
            ) from e
        cam = None
        try:
            # 연결된 카메라가 없으면 Picamera2()가 IndexError/RuntimeError를 던진다.
            cam = Picamera2()
            # libcamera의 'RGB888'은 메모리상 B,G,R 순서라 OpenCV(BGR)와 그대로 맞는다.
            config = cam.create_video_configuration(main={"size": (self.width, self.height), "format": "RGB888"})
            cam.configure(config)
            cam.start()
        except Exception as e:
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
            raise CameraError(
                f"카메라 모듈을 열 수 없습니다({e}). 케이블 연결을 확인하고 'rpicam-hello --list-cameras'로 "
                "카메라가 보이는지 확인해 주세요."
            ) from e
        self._cam = cam
        log.info("카메라 모듈 열림 (%dx%d)", self.width, self.height)

    def read(self) -> Optional[np.ndarray]:
        if self._cam is None:
            return None
        try:
            frame = self._cam.capture_array()
        except Exception as e:
            log.warning("카메라 모듈 프레임 읽기 실패: %s", e)
            return None
        if frame is None:
            return None
        # 설정에 따라 4채널(XBGR8888)로 올 수 있어 3채널 BGR로 맞춘다.
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        return frame

    def close(self) -> None:
        cam, self._cam = self._cam, None
        if cam is None:
            return
        try:
            cam.stop()
        except Exception:
            pass
        try:
            cam.close()
        except Exception as e:
            log.warning("카메라 모듈 닫기 실패: %s", e)


class OpenCVCamera(Camera):
    """USB 웹캠 (cv2.VideoCapture)."""

    name = "opencv"

    def __init__(self, index: int, width: int, height: int):
        self.index = index
        self.width = width
        self.height = height
        self._cap = None

    def open(self) -> None:
        if self._cap is not None:
            return
        cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            raise CameraError(
                f"USB 카메라 {self.index}번을 열 수 없습니다. 'ls /dev/video*'로 장치가 있는지, "
                "PI_CAMERA_INDEX가 맞는지 확인해 주세요."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # 버퍼에 쌓인 옛 프레임 대신 가장 최근 프레임을 받도록 버퍼를 줄인다(지원하지 않는 장치는 무시).
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # 카메라 모듈만 달린 Pi에서도 /dev/video0은 열리지만 프레임이 나오지 않는다.
        # 그래서 한 장을 실제로 읽어 봐야 'auto'가 올바른 백엔드를 고를 수 있다.
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise CameraError(f"USB 카메라 {self.index}번이 열렸지만 프레임이 나오지 않습니다.")
        self._cap = cap
        log.info("USB 카메라 %d번 열림 (%dx%d)", self.index, frame.shape[1], frame.shape[0])

    def read(self) -> Optional[np.ndarray]:
        if self._cap is None:
            return None
        try:
            ok, frame = self._cap.read()
        except cv2.error as e:
            log.warning("USB 카메라 프레임 읽기 실패: %s", e)
            return None
        return frame if ok else None

    def close(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()


class VideoFileCamera(Camera):
    """시험용 영상 파일. 끝나면 처음부터 다시 재생한다. 속도(fps) 맞추기는 호출하는 쪽의 몫이다."""

    name = "file"

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._cap = None

    def open(self) -> None:
        if self._cap is not None:
            return
        if not self.path.is_file():
            raise CameraError(f"시험용 영상 파일이 없습니다: {self.path} (PI_VIDEO_PATH 확인)")
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"영상 파일을 열 수 없습니다(코덱 확인): {self.path}")
        self._cap = cap
        log.info("시험용 영상 열림: %s", self.path)

    def read(self) -> Optional[np.ndarray]:
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        if not ok:
            # 파일 끝: 처음으로 되감고 한 번 더 읽는다. 그래도 안 되면 읽을 프레임이 없는 파일이다.
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self._cap.read()
        return frame if ok else None

    def close(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()


# ---------------------------------------------------------------------------
# FrameSource: 클라이언트가 쓰는 인터페이스
# ---------------------------------------------------------------------------

class FrameSource:
    """보낼 JPEG 바이트를 한 장씩 꺼낸다. next_jpeg()는 예외를 던지지 않고, 보낼 것이 없으면 None."""

    name = "base"

    def open(self) -> None:
        pass

    def next_jpeg(self) -> Optional[bytes]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class CameraFrameSource(FrameSource):
    """Camera + JPEG 인코딩. 읽기가 연달아 실패하면 카메라를 닫고 몇 초마다 다시 연다.

    다시 여는 동안에도 next_jpeg()는 None만 돌려줄 뿐 예외를 던지지 않는다.
    그래야 클라이언트가 WebSocket 연결을 유지한 채 프레임만 멈추고,
    서버는 프레임이 끊긴 것을 보고 '카메라 응답 없음'을 표시한다.
    """

    def __init__(
        self,
        camera: Camera,
        quality: int,
        width: int,
        height: int,
        max_failures: int = 5,
        reopen_interval: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.camera = camera
        self.name = camera.name
        self.quality = quality
        self.width = width
        self.height = height
        self.max_failures = max_failures
        self.reopen_interval = reopen_interval
        self._clock = clock
        self._opened = False
        self._failures = 0
        self._last_open_attempt = float("-inf")
        self._last_reopen_warning = float("-inf")

    @property
    def is_open(self) -> bool:
        return self._opened

    def open(self) -> None:
        """카메라를 연다. 실패하면 CameraError (시작할 때 카메라 문제를 바로 알리기 위해)."""
        if self._opened:
            return
        self._last_open_attempt = self._clock()
        self.camera.open()
        self._opened = True
        self._failures = 0

    def next_jpeg(self) -> Optional[bytes]:
        if not self._opened and not self._try_reopen():
            return None
        try:
            frame = self.camera.read()
            data = encode_jpeg(frame, self.quality, self.width, self.height) if frame is not None else None
        except Exception as e:  # 장치 드라이버 예외가 클라이언트 루프를 죽이지 않도록
            log.warning("프레임 처리 실패: %s", e)
            data = None
        if data is None:
            self._on_failure()
            return None
        self._failures = 0
        return data

    def close(self) -> None:
        self._opened = False
        self.camera.close()

    def _on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.max_failures:
            log.error("카메라 프레임을 %d번 연속 읽지 못했습니다. 카메라를 닫고 %.0f초마다 다시 엽니다.",
                      self._failures, self.reopen_interval)
            self.close()
            self._failures = 0
            self._last_open_attempt = self._clock()

    def _try_reopen(self) -> bool:
        now = self._clock()
        if now - self._last_open_attempt < self.reopen_interval:
            return False
        self._last_open_attempt = now
        try:
            self.camera.open()
        except CameraError as e:
            # 몇 초마다 시도하므로 경고는 1분에 한 번만 남긴다.
            if now - self._last_reopen_warning >= 60.0:
                log.warning("카메라 다시 열기 실패: %s", e)
                self._last_reopen_warning = now
            return False
        except Exception as e:
            log.warning("카메라 다시 열기 중 예외: %s", e)
            return False
        self._opened = True
        self._failures = 0
        self._last_reopen_warning = float("-inf")
        log.info("카메라를 다시 열었습니다 (%s)", self.name)
        return True


FakeFrames = Union[Sequence[bytes], Callable[[], Optional[bytes]]]


class FakeFrameSource(FrameSource):
    """테스트·시연용. 정해 둔 바이트를 그대로 내보낸다.

    frames가 목록이면 차례로 내보내고, 다 쓰면 마지막 프레임을 계속 반복한다
    (예: 서 있음 → 넘어짐 → 누운 채 유지). loop=True면 처음부터 다시 돈다.
    빈 목록이면 항상 None(카메라 고장 흉내).
    frames가 함수면 부를 때마다 그 반환값(None 가능)을 내보낸다.
    """

    name = "fake"

    def __init__(self, frames: FakeFrames, loop: bool = False):
        self._func: Optional[Callable[[], Optional[bytes]]] = frames if callable(frames) else None
        self._frames: List[bytes] = [] if callable(frames) else list(frames)
        self._loop = loop
        self._index = 0
        self.sent = 0  # 내보낸 프레임 수 (테스트 확인용)

    def next_jpeg(self) -> Optional[bytes]:
        if self._func is not None:
            data = self._func()
        elif not self._frames:
            data = None
        else:
            if self._index >= len(self._frames):
                self._index = 0 if self._loop else len(self._frames) - 1
            data = self._frames[self._index]
            self._index += 1
        if data is not None:
            self.sent += 1
        return data


# server/detection/pose.py의 fake_frame('standing')과 같은 모양.
# Pi에는 서버 패키지를 import하지 않으므로 여기에 그대로 적어 둔다(테스트에서 둘이 같은지 확인한다).
_FAKE_STANDING = json.dumps({
    "ls": [305.0, 200.0], "rs": [335.0, 200.0],
    "lh": [310.0, 300.0], "rh": [330.0, 300.0],
    "bbox": [290.0, 150.0, 350.0, 450.0],
}).encode("utf-8")


def create_frame_source(settings, fake_frames: Optional[FakeFrames] = None) -> FrameSource:
    """설정(camera_backend)에 맞는 FrameSource를 만들어 연 상태로 돌려준다.

    'auto'는 카메라 모듈(picamera2)을 먼저 시도하고, 안 되면 USB 웹캠(opencv)을 쓴다.
    아무것도 열리지 않으면 이유를 담은 CameraError.
    'fake'는 fake_frames(없으면 '서 있는 사람' 프레임 반복)를 내보낸다. 서버의 FALL_POSE_BACKEND=fake와 함께 쓴다.
    """
    backend = (settings.camera_backend or "auto").strip().lower()
    w, h = settings.frame_width, settings.frame_height

    if backend == "fake":
        log.warning("가짜 카메라를 사용합니다(테스트 전용). 실제 영상은 보내지 않습니다.")
        return FakeFrameSource(fake_frames if fake_frames is not None else [_FAKE_STANDING])

    if backend == "file":
        if not settings.video_path:
            raise CameraError("PI_CAMERA=file이면 PI_VIDEO_PATH에 영상 파일 경로를 넣어 주세요.")
        candidates: List[Camera] = [VideoFileCamera(settings.video_path)]
    elif backend == "picamera2":
        candidates = [Picamera2Camera(w, h)]
    elif backend == "opencv":
        candidates = [OpenCVCamera(settings.camera_index, w, h)]
    elif backend == "auto":
        candidates = [Picamera2Camera(w, h), OpenCVCamera(settings.camera_index, w, h)]
    else:
        raise CameraError(
            f"알 수 없는 카메라 종류: {settings.camera_backend!r} (auto, picamera2, opencv, file, fake 중 하나)"
        )

    errors: List[CameraError] = []
    for camera in candidates:
        source = CameraFrameSource(camera, settings.jpeg_quality, w, h)
        try:
            source.open()
        except CameraError as e:
            log.info("%s 카메라 사용 불가: %s", camera.name, e)
            errors.append(e)
            continue
        log.info("카메라 백엔드: %s", camera.name)
        return source

    if len(errors) == 1:
        raise errors[0]
    lines = [f"- {camera.name}: {e}" for camera, e in zip(candidates, errors)]
    raise CameraError("카메라를 찾지 못했습니다.\n" + "\n".join(lines)
                      + "\n카메라 없이 시험하려면 PI_CAMERA=fake를 쓰세요.")
