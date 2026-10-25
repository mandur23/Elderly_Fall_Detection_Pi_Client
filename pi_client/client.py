"""서버 접속 루프: 카메라 프레임 전송, 서버 메시지 처리, 음성 확인 실행, 재접속.

흐름
- 연결 하나에 작업 두 개: 프레임 보내기(_send_frames)와 서버 메시지 받기(_receive).
  어느 쪽이든 끝나면(연결 끊김) 둘 다 정리하고 재접속 간격(reconnect_delays)만큼 쉬었다가 다시 접속한다.
- 음성 확인은 연결과 따로 돈다. 녹음 도중 연결이 잠깐 끊겨도 녹음·업로드(HTTP)는 끝까지 한다.
  서버는 다시 접속한 기기에 같은 voice_check를 다시 보내는데, 같은 요청이 이미 진행 중이면 무시한다.
- 4401/4403(기기 키 오류·권한 없음), 4409(같은 기기의 새 연결로 교체됨)는 다시 접속해도 소용없으므로
  ClientStop을 던져 끝낸다. __main__이 종료 코드로 바꾸고, systemd는 그 코드면 재시작하지 않는다.

종료 코드는 pi_client/deploy/pi-client.service의 RestartPreventExitStatus=2 3 4와 맞춰야 한다.
"""

import asyncio
import json
import logging
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
# websockets 13은 websockets.connect가 아직 옛 구현(extra_headers)이라 새 구현을 직접 불러온다(14부터는 같다).
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from pi_client.audio import AudioDevice
from pi_client.camera import CameraError, FrameSource
from pi_client.voice_check import run_voice_check

log = logging.getLogger("pi.client")

# 종료 코드 (README '서비스와 종료 코드'와 같은 값)
EXIT_CONFIG = 2    # 설정이 잘못됨(__main__에서도 씀)
EXIT_AUTH = 3      # 4401 기기 키 오류, 4403 권한 없음
EXIT_REPLACED = 4  # 4409 같은 기기로 더 새로운 연결이 들어옴

CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_REPLACED = 4409

# 접속(TLS 포함)을 기다리는 최대 시간. 넘으면 실패로 보고 재접속 간격만큼 쉰다.
OPEN_TIMEOUT = 10.0
# 연결이 반쯤 끊긴 상태(Wi-Fi 끊김 등)를 알아채기 위한 ping. 응답이 없으면 연결을 닫고 다시 접속한다.
PING_INTERVAL = 20.0
PING_TIMEOUT = 20.0
# 프레임 한 장 전송의 최대 시간. 연결이 반쯤 끊기면 TCP 버퍼가 차서 send()가 끝나지 않는데,
# 이때는 ping도 같은 버퍼 비우기(drain)에서 막혀 PING_TIMEOUT이 시작되지도 않는다.
# 그래서 전송 시간을 따로 재서 넘으면 연결을 끊고 다시 접속한다.
SEND_TIMEOUT = 20.0
# 연결을 닫을 때(종료·재접속) 닫기 인사를 기다리는 최대 시간. 버퍼가 막혀 있으면 close()도 끝나지 않는다.
CLOSE_TIMEOUT = 3.0
# 음성 업로드 HTTP 시간 제한
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

VoiceKey = Tuple[int, int]  # (event_id, attempt)


class ClientStop(Exception):
    """다시 접속해도 소용없어 클라이언트를 끝내야 함. code는 프로세스 종료 코드."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def make_ssl_context(settings) -> ssl.SSLContext:
    """서버 인증서 확인용. PI_CA_FILE(mkcert rootCA.pem)이 있으면 그것으로, 없으면 시스템 인증서로 확인한다."""
    if settings.ca_file:
        return ssl.create_default_context(cafile=settings.ca_file)
    return ssl.create_default_context()


def _abort(ws) -> None:
    """닫기 인사 없이 TCP 연결을 바로 끊는다. 막힌 send()·recv()·close()가 ConnectionClosed로 풀린다."""
    ws.transport.abort()


def _close_code(exc: ConnectionClosed) -> Optional[int]:
    """서버가 보낸 닫기 코드. 닫기 프레임 없이 끊겼으면 None."""
    rcvd = getattr(exc, "rcvd", None)
    return rcvd.code if rcvd is not None else None


class RetryingFrameSource(FrameSource):
    """시작할 때 카메라가 열리지 않았을 때 쓰는 FrameSource.

    카메라가 없다고 프로그램을 끝내면 서버 연결도 끊겨 보호자 화면에는 '기기 꺼짐'으로 보인다.
    연결은 유지하고 프레임만 보내지 않으면 서버가 '카메라 응답 없음'을 표시하므로 원인이 더 분명하다.
    그동안 interval초마다 카메라를 다시 만들어 본다.
    """

    name = "retrying"

    def __init__(self, factory: Callable[[], FrameSource], interval: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self._factory = factory
        self._interval = interval
        self._clock = clock
        self._source: Optional[FrameSource] = None
        self._last_try = float("-inf")
        self._last_warning = float("-inf")

    def next_jpeg(self) -> Optional[bytes]:
        if self._source is None:
            now = self._clock()
            if now - self._last_try < self._interval:
                return None
            self._last_try = now
            try:
                self._source = self._factory()
            except CameraError as e:
                if now - self._last_warning >= 60.0:  # 몇 초마다 시도하므로 경고는 1분에 한 번
                    log.warning("카메라를 아직 열 수 없습니다: %s", e)
                    self._last_warning = now
                return None
            log.info("카메라를 열었습니다 (%s). 프레임 전송을 시작합니다.", self._source.name)
        return self._source.next_jpeg()

    def close(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            source.close()


class PiClient:
    """서버와의 연결을 유지하며 프레임을 보내고 음성 확인 요청을 처리한다.

    connect: websockets.asyncio.client.connect와 같은 모양의 함수(테스트에서 가짜로 바꾼다).
    http: 음성 업로드용 httpx.AsyncClient. 없으면 run()이 만들고 끝날 때 닫는다.
    voice_check: run_voice_check와 같은 모양의 함수(테스트용).
    """

    def __init__(self, settings, frame_source: FrameSource, audio: AudioDevice,
                 connect: Callable[..., Any] = ws_connect,
                 http: Optional[httpx.AsyncClient] = None,
                 voice_check: Callable[..., Any] = run_voice_check):
        self.settings = settings
        self.frame_source = frame_source
        self.audio = audio
        self._connect = connect
        self._http = http
        self._voice_check = voice_check

        self._stop_event = asyncio.Event()
        self._stopping = False
        self._ssl: Optional[ssl.SSLContext] = None
        self._camera_executor: Optional[ThreadPoolExecutor] = None
        self._voice_tasks: Dict[VoiceKey, asyncio.Task] = {}
        self._voice_lock = asyncio.Lock()  # 스피커·마이크는 하나이므로 음성 확인은 한 번에 하나씩
        self._latest_voice_key: Optional[VoiceKey] = None
        self._hello_received = False

        # 상태(로그·테스트 확인용)
        self.state: Optional[str] = None
        self.connected = False
        self.connections = 0  # hello까지 받은 연결 수
        self.frames_sent = 0
        self.voice_results: List[Tuple[VoiceKey, str]] = []

    # --- 바깥에서 부르는 것 -------------------------------------------------

    def stop(self) -> None:
        """run()을 끝내게 한다(SIGTERM·SIGINT 처리기, 테스트). 이벤트 루프 스레드에서 부른다."""
        self._stopping = True
        self._stop_event.set()

    async def run(self) -> None:
        """stop()이 불리거나 ClientStop이 날 때까지 접속을 유지한다."""
        settings = self.settings
        try:
            self._ssl = make_ssl_context(settings)
        except (OSError, ssl.SSLError) as e:
            raise ClientStop(EXIT_CONFIG, f"인증 기관 파일(PI_CA_FILE={settings.ca_file})을 읽을 수 없습니다: {e}")
        own_http = self._http is None
        if own_http:
            self._http = httpx.AsyncClient(verify=self._ssl, timeout=HTTP_TIMEOUT)
        # 카메라 읽기는 스레드 하나에서만 한다(장치 객체를 동시에 쓰지 않도록, 종료 때 읽기가 끝나기를 기다리도록).
        self._camera_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pi-camera")
        delays = list(settings.reconnect_delays) or [5.0]
        failures = 0
        try:
            while not self._stopping:
                self._hello_received = False
                try:
                    await self._session()
                except ConnectionClosed as e:
                    self._on_closed(e)
                except InvalidURI as e:
                    raise ClientStop(EXIT_CONFIG, f"서버 주소가 잘못되었습니다(PI_SERVER_URL): {e}")
                except InvalidStatus as e:
                    # 우리 서버는 접속을 받은 뒤 닫기 코드로 거부하므로, HTTP 거부는 주소·프록시 문제다.
                    log.warning("서버가 WebSocket 접속을 HTTP %s로 거부했습니다. PI_SERVER_URL을 확인해 주세요.",
                                e.response.status_code)
                except ssl.SSLCertVerificationError as e:
                    log.warning("서버 인증서를 확인할 수 없습니다: %s. PI_CA_FILE, 인증서에 서버 IP가 들어 있는지, "
                                "Pi 시계가 맞는지 확인해 주세요.", e.verify_message or e)
                except (OSError, asyncio.TimeoutError, TimeoutError, InvalidHandshake) as e:
                    log.warning("서버에 접속할 수 없습니다 (%s): %s", settings.server_url, str(e) or type(e).__name__)
                finally:
                    self.connected = False
                if self._stopping:
                    break
                if self._hello_received:
                    failures = 0  # 정상 접속했다가 끊긴 것: 처음 간격부터 다시
                delay = delays[min(failures, len(delays) - 1)]
                failures += 1
                log.info("%.0f초 뒤 다시 접속합니다", delay)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._shutdown(own_http)

    # --- 연결 하나 ----------------------------------------------------------

    async def _session(self) -> None:
        settings = self.settings
        url = settings.ws_url
        kwargs: Dict[str, Any] = {
            # 기기 키는 주소가 아니라 머리글로 보낸다(서버·프록시 접속 로그에 남지 않도록).
            "additional_headers": {"X-Device-Key": settings.device_key},
            "open_timeout": OPEN_TIMEOUT,
            "ping_interval": PING_INTERVAL,
            "ping_timeout": PING_TIMEOUT,
        }
        if url.startswith("wss://"):
            kwargs["ssl"] = self._ssl
        log.info("서버에 접속합니다: %s", url)
        async with self._connect(url, **kwargs) as ws:
            sender = asyncio.create_task(self._send_frames(ws), name="pi-send-frames")
            receiver = asyncio.create_task(self._receive(ws), name="pi-receive")
            stopper = asyncio.create_task(self._stop_event.wait(), name="pi-stop-wait")
            tasks = (receiver, sender, stopper)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # async with를 나갈 때의 close()는 버퍼가 막혀 있으면 끝나지 않는다(SIGTERM 후 systemd가 강제 종료).
                # 여기서 시간 제한을 두고 먼저 닫는다. 이미 닫힌 연결이면 나갈 때의 close()는 바로 끝난다.
                await self._close_ws(ws)
            # 받기 쪽 예외를 먼저 본다. 서버가 보낸 닫기 코드가 담겨 있다.
            for task in (receiver, sender):
                if task in done and not task.cancelled() and task.exception() is not None:
                    raise task.exception()

    async def _close_ws(self, ws) -> None:
        try:
            await asyncio.wait_for(ws.close(), CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("연결을 %.0f초 안에 닫지 못해 바로 끊습니다", CLOSE_TIMEOUT)
            _abort(ws)

    def _on_closed(self, e: ConnectionClosed) -> None:
        code = _close_code(e)
        device_id = self.settings.device_id
        if code == CLOSE_UNAUTHORIZED:
            raise ClientStop(EXIT_AUTH, (
                f"서버가 기기 인증을 거부했습니다(4401). PI_DEVICE_ID({device_id})와 PI_DEVICE_KEY가 맞는지, "
                "웹 마이페이지에서 이 기기를 지우지 않았는지 확인해 주세요."))
        if code == CLOSE_FORBIDDEN:
            raise ClientStop(EXIT_AUTH, f"서버가 접속을 거부했습니다(4403 권한 없음). 기기 {device_id}의 설정을 확인해 주세요.")
        if code == CLOSE_REPLACED:
            raise ClientStop(EXIT_REPLACED, (
                f"같은 기기(ID {device_id})로 더 새로운 연결이 들어와 이 연결이 끊겼습니다(4409). "
                "클라이언트가 두 개 실행 중인지(서비스와 터미널, 같은 키를 쓰는 다른 Pi) 확인해 주세요."))
        if code == CLOSE_BAD_REQUEST:
            log.error("서버가 요청이 잘못되었다고 연결을 닫았습니다(4400): %s. 다시 접속합니다.", e.rcvd.reason)
        else:
            log.warning("서버 연결이 끊겼습니다 (코드 %s)", code)

    async def _send_frames(self, ws) -> None:
        """fps에 맞춰 프레임을 한 장씩 보낸다.

        한 장을 다 보낸 뒤에 다음 장을 찍으므로 보낼 프레임이 쌓이지 않는다. 전송이 느려 시간이 밀리면
        밀린 만큼 몰아 보내지 않고 건너뛴다. 서버는 지금 장면을 봐야 낙상을 제때 판단할 수 있다.
        카메라가 고장이면 next_jpeg()가 None을 주므로 연결은 유지한 채 보내지 않는다.
        """
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.settings.fps
        next_at = loop.time()
        while True:
            data = await loop.run_in_executor(self._camera_executor, self._capture)
            if data is not None:
                try:
                    await asyncio.wait_for(ws.send(data), SEND_TIMEOUT)
                except asyncio.TimeoutError:
                    # 상대가 받지 않는다(Wi-Fi 끊김 등으로 연결이 반쯤 끊김). ping으로는 알아채지 못하므로 여기서 끊는다.
                    log.warning("프레임 전송이 %.0f초 넘게 끝나지 않습니다. 연결을 끊고 다시 접속합니다.", SEND_TIMEOUT)
                    _abort(ws)
                    raise ConnectionError(f"프레임 전송 {SEND_TIMEOUT:.0f}초 초과") from None
                self.frames_sent += 1
            next_at += interval
            now = loop.time()
            if next_at < now:
                next_at = now
            await asyncio.sleep(next_at - now)

    def _capture(self) -> Optional[bytes]:
        """카메라 스레드에서 실행. 예상 못 한 예외가 나도 전송 작업(연결)이 죽지 않게 None으로 바꾼다."""
        try:
            return self.frame_source.next_jpeg()
        except Exception as e:
            log.warning("프레임 읽기 중 예외: %r", e)
            return None

    async def _receive(self, ws) -> None:
        while True:
            raw = await ws.recv()
            if not isinstance(raw, str):
                continue  # 서버는 바이너리를 보내지 않는다
            try:
                message = json.loads(raw)
            except ValueError:
                log.debug("JSON이 아닌 메시지 무시: %.100s", raw)
                continue
            if isinstance(message, dict):
                self._handle_message(message)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "hello":
            self._hello_received = True
            self.connected = True
            self.connections += 1
            log.info("서버 접속 완료: 기기 %s (%s)", message.get("device_id"), message.get("name"))
        elif kind == "state":
            state = message.get("state")
            if state != self.state:
                log.info("기기 상태: %s -> %s", self.state, state)
            self.state = state
        elif kind == "voice_check":
            self._start_voice_check(message)
        else:
            log.debug("알 수 없는 서버 메시지: %s", kind)

    # --- 음성 확인 ----------------------------------------------------------

    def _start_voice_check(self, message: Dict[str, Any]) -> None:
        try:
            key: VoiceKey = (int(message["event_id"]), int(message["attempt"]))
        except (KeyError, TypeError, ValueError):
            log.warning("잘못된 음성 확인 요청 무시: %s", message)
            return
        running = self._voice_tasks.get(key)
        if running is not None and not running.done():
            # 다시 접속하면 서버가 같은 요청을 또 보낸다. 이미 녹음 중이면 그대로 둔다.
            log.info("이미 진행 중인 음성 확인 요청(이벤트 %s, %s회차) 무시", *key)
            return
        self._latest_voice_key = key
        task = asyncio.create_task(self._voice_check_task(key), name=f"pi-voice-{key[0]}-{key[1]}")
        self._voice_tasks[key] = task
        task.add_done_callback(lambda t, k=key: self._voice_done(k, t))

    async def _voice_check_task(self, key: VoiceKey) -> Optional[str]:
        async with self._voice_lock:
            if self._latest_voice_key != key:
                # 앞 확인이 끝나기를 기다리는 사이 새 요청이 왔다. 서버는 새 회차만 기다리므로 건너뛴다.
                log.info("더 새로운 음성 확인 요청이 있어 이벤트 %s %s회차는 건너뜁니다", *key)
                return None
            outcome = await self._voice_check(self.settings, self.audio, self._http, key[0], key[1])
            self.voice_results.append((key, outcome))
            return outcome

    def _voice_done(self, key: VoiceKey, task: asyncio.Task) -> None:
        if self._voice_tasks.get(key) is task:
            del self._voice_tasks[key]
        if not task.cancelled() and task.exception() is not None:
            log.error("음성 확인 중 예외 (이벤트 %s, %s회차)", *key, exc_info=task.exception())

    # --- 정리 ---------------------------------------------------------------

    async def _shutdown(self, own_http: bool) -> None:
        tasks = list(self._voice_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            log.info("진행 중인 음성 확인 %d개를 취소합니다", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._camera_executor is not None:
            # 읽고 있던 프레임이 끝나기를 기다린다. 그 뒤에야 카메라를 안전하게 닫을 수 있다.
            executor, self._camera_executor = self._camera_executor, None
            await asyncio.get_running_loop().run_in_executor(None, executor.shutdown, True)
        if own_http and self._http is not None:
            await self._http.aclose()
            self._http = None
