"""Pi 클라이언트 핵심(client.py, voice_check.py, __main__.py) 테스트.

- 단위 테스트: 가짜 WebSocket(FakeConnect)과 httpx.MockTransport로 접속·재접속·종료 코드·프레임 전송·
  음성 업로드 형식을 확인한다.
- 끝까지 테스트: 실제 서버(server.main.create_app, 가짜 자세 추정·가짜 음성 인식)를 uvicorn으로 띄우고
  PiClient를 붙인다. 가짜 카메라는 서 있다가 넘어져 누운 자세의 관절 좌표 JSON을, 가짜 오디오는
  정해 둔 문장을 보낸다(서버의 가짜 음성 인식기가 바이트를 문장으로 읽는다).

비동기 테스트는 asyncio.run으로 돌린다(pytest-asyncio 없이).
"""

import asyncio
import socket
import threading
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from pi_client import __main__ as main_mod
from pi_client import client as client_mod
from pi_client import voice_check as vc
from pi_client.audio import FakeAudio
from pi_client.camera import CameraError, FakeFrameSource, FrameSource
from pi_client.client import ClientStop, PiClient, RetryingFrameSource
from pi_client.config import ClientSettings

KEY = "test-device-key"


def make_settings(**overrides) -> ClientSettings:
    values = dict(
        server_url="http://pi-test.invalid",
        device_id=7,
        device_key=KEY,
        ca_file=None,
        reconnect_delays=[0.01],
        camera_backend="fake",
        camera_index=0,
        video_path=None,
        fps=50.0,
        audio_backend="fake",
        prompt_wav=Path("/nonexistent/ask_status.wav"),
        record_seconds=0.1,
        input_device=None,
        output_device=None,
    )
    values.update(overrides)
    return ClientSettings(**values)


def run(coro, timeout=10.0):
    async def _with_timeout():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(_with_timeout())


async def until(predicate, timeout=3.0, interval=0.01, what="조건"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}이(가) 시간 안에 만족되지 않음")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# 가짜 WebSocket
# ---------------------------------------------------------------------------

def closed(code, reason=""):
    return ConnectionClosedError(Close(code, reason), None)


def hello(device_id=7):
    return '{"type":"hello","device_id":%d,"name":"거실 Pi","kind":"pi"}' % device_id


class FakeTransport:
    def __init__(self, ws):
        self.ws = ws
        self.aborted = False

    def abort(self):
        # 실제 연결처럼 막혀 있던 recv()가 연결 끊김으로 풀린다.
        self.aborted = True
        self.ws.push(closed(1006))


class FakeWS:
    """서버 흉내. incoming에 넣은 문자열을 recv()로 주고, 예외를 넣으면 그 예외를 던진다."""

    def __init__(self, messages=(), send_delay=0.0):
        self.incoming: asyncio.Queue = asyncio.Queue()
        for m in messages:
            self.incoming.put_nowait(m)
        self.send_delay = send_delay
        self.sent = []  # (보낸 시각, 데이터)
        self.transport = FakeTransport(self)
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1

    def push(self, message):
        self.incoming.put_nowait(message)

    async def send(self, data):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent.append((time.monotonic(), data))

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item


class _ConnectCM:
    def __init__(self, item):
        self.item = item

    async def __aenter__(self):
        if isinstance(self.item, BaseException):
            raise self.item
        return self.item

    async def __aexit__(self, *exc):
        return False


class FakeConnect:
    """websockets.connect 대신. plan의 항목을 접속할 때마다 하나씩 쓴다(예외면 접속 실패)."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []  # (url, kwargs, 시각)

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs, time.monotonic()))
        item = self.plan.pop(0) if self.plan else OSError("더 이상 접속 계획 없음")
        return _ConnectCM(item)


class SlowVoiceCheck:
    """run_voice_check 대신. release될 때까지 끝나지 않는다."""

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()
        self.cancelled = 0

    async def __call__(self, settings, audio, http, event_id, attempt):
        self.calls.append((event_id, attempt))
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return vc.UPLOADED


# ---------------------------------------------------------------------------
# voice_check
# ---------------------------------------------------------------------------

def parse_multipart(request: httpx.Request):
    """{필드 이름: (파일 이름, 내용 형식, 바이트)}"""
    assert request.headers["content-type"].startswith("multipart/form-data")
    raw = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    message = BytesParser(policy=policy.HTTP).parsebytes(raw)
    parts = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = (part.get_filename(), part.get("content-type"), part.get_payload(decode=True))
    return parts


class Recorder:
    """MockTransport 처리기. 요청을 모으고 정해 둔 응답을 차례로 돌려준다(마지막 응답 반복)."""

    def __init__(self, *responses):
        self.requests = []
        self.responses = list(responses) or [httpx.Response(200, json={"accepted": True})]

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def voice(settings, audio, handler, event_id=12, attempt=2):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await vc.run_voice_check(settings, audio, http, event_id, attempt)
    return run(go())


@pytest.fixture
def prompt(tmp_path):
    path = tmp_path / "ask_status.wav"
    path.write_bytes(b"RIFF....WAVE")  # FakeAudio는 내용을 읽지 않는다
    return path


def test_voice_check_uploads_recording_as_multipart(prompt):
    settings = make_settings(prompt_wav=prompt, record_seconds=3.5)
    audio = FakeAudio(["네 괜찮아요"])
    handler = Recorder()

    assert voice(settings, audio, handler) == vc.UPLOADED

    assert audio.played == [prompt]  # 재생이 끝난 뒤 녹음
    assert audio.recorded == [3.5]
    [request] = handler.requests
    assert request.method == "POST"
    assert str(request.url) == "http://pi-test.invalid/api/events/12/voice"
    assert request.headers["X-Device-Key"] == KEY
    assert KEY not in str(request.url)
    parts = parse_multipart(request)
    assert set(parts) == {"audio", "attempt"}
    assert parts["audio"] == ("reply.wav", "audio/wav", "네 괜찮아요".encode("utf-8"))
    assert parts["attempt"][2] == b"2"


def test_voice_check_reports_error_without_audio_when_recording_fails(prompt):
    settings = make_settings(prompt_wav=prompt)
    handler = Recorder()

    assert voice(settings, FakeAudio([None]), handler) == vc.REPORTED_ERROR

    parts = parse_multipart(handler.requests[0])
    assert set(parts) == {"error", "attempt"}
    filename, _, error = parts["error"]
    assert filename is None  # 파일이 아니라 일반 필드
    assert "녹음 실패" in error.decode("utf-8")
    assert parts["attempt"][2] == b"2"


def test_voice_check_still_records_when_prompt_missing_or_playback_fails(prompt, caplog):
    # 안내 음성 파일이 없다
    audio = FakeAudio(["괜찮아"])
    handler = Recorder()
    assert voice(make_settings(), audio, handler) == vc.UPLOADED
    assert audio.played == [] and len(audio.recorded) == 1
    assert "안내 음성 파일이 없습니다" in caplog.text

    # 스피커 재생 실패
    audio = FakeAudio(["괜찮아"], play_error=True)
    handler = Recorder()
    assert voice(make_settings(prompt_wav=prompt), audio, handler) == vc.UPLOADED
    assert audio.played == [prompt] and len(audio.recorded) == 1
    assert "audio" in parse_multipart(handler.requests[0])


def test_voice_check_outcomes_for_server_answers(prompt, monkeypatch):
    monkeypatch.setattr(vc, "UPLOAD_RETRY_DELAY", 0.0)
    settings = make_settings(prompt_wav=prompt)

    not_waiting = Recorder(httpx.Response(200, json={"accepted": False}))
    assert voice(settings, FakeAudio(["네"]), not_waiting) == vc.IGNORED

    server_error = Recorder(httpx.Response(500, text="boom"))
    assert voice(settings, FakeAudio(["네"]), server_error) == vc.HTTP_ERROR
    assert len(server_error.requests) == 1  # HTTP 응답은 다시 보내지 않는다

    flaky = Recorder(httpx.ConnectError("잠깐 끊김"), httpx.Response(200, json={"accepted": True}))
    assert voice(settings, FakeAudio(["네"]), flaky) == vc.UPLOADED
    assert len(flaky.requests) == 2

    down = Recorder(httpx.ConnectError("끊김"))
    assert voice(settings, FakeAudio(["네"]), down) == vc.NETWORK_ERROR
    assert len(down.requests) == vc.UPLOAD_TRIES


class HangingAudio(FakeAudio):
    """장치가 멈춘 오디오: hang_play/hang_record 횟수만큼 release될 때까지 돌아오지 않는다(녹음 중 USB 뽑힘 흉내)."""

    def __init__(self, replies=None, hang_play=0, hang_record=0):
        super().__init__(replies)
        self.hang_play = hang_play
        self.hang_record = hang_record
        self.release = threading.Event()

    def play_wav(self, path):
        super().play_wav(path)
        if self.hang_play > 0:
            self.hang_play -= 1
            self.release.wait(10)

    def record(self, seconds, sample_rate):
        if self.hang_record > 0:
            self.hang_record -= 1
            self.release.wait(10)
            return b""
        return super().record(seconds, sample_rate)


def test_voice_check_stuck_device_reports_error_instead_of_hanging(prompt, monkeypatch):
    monkeypatch.setattr(vc, "DEVICE_TIMEOUT_MARGIN", 0.2)
    monkeypatch.setattr(vc, "DEFAULT_PROMPT_SECONDS", 0.05)  # prompt는 내용 없는 가짜 WAV라 길이를 모른다
    settings = make_settings(prompt_wav=prompt, record_seconds=0.05)
    audio = HangingAudio(hang_play=1, hang_record=1)
    handler = Recorder()
    try:
        assert voice(settings, audio, handler) == vc.REPORTED_ERROR
    finally:
        audio.release.set()
    parts = parse_multipart(handler.requests[0])
    assert set(parts) == {"error", "attempt"}
    assert "녹음 시간 초과" in parts["error"][2].decode("utf-8")


def test_stuck_voice_check_releases_lock_for_next_request(prompt, monkeypatch):
    """한 번 장치가 멈춰도 음성 확인 잠금이 풀려 다음 요청(다음 회차·다음 낙상)은 정상으로 한다."""
    monkeypatch.setattr(vc, "DEVICE_TIMEOUT_MARGIN", 0.2)
    audio = HangingAudio(["괜찮아요"], hang_record=1)
    handler = Recorder()

    async def go():
        ws = FakeWS([hello()])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = PiClient(make_settings(prompt_wav=prompt, record_seconds=0.05), FakeFrameSource([]), audio,
                              connect=FakeConnect([ws]), http=http)
            task = asyncio.create_task(client.run())
            await until(lambda: client.connected)
            ws.push('{"type":"voice_check","event_id":4,"attempt":1}')
            await until(lambda: client.voice_results, what="1회차 결과")
            ws.push('{"type":"voice_check","event_id":4,"attempt":2}')
            await until(lambda: len(client.voice_results) == 2, what="2회차 결과")
            client.stop()
            await task
            return client

    try:
        client = run(go())
    finally:
        audio.release.set()
    assert client.voice_results == [((4, 1), vc.REPORTED_ERROR), ((4, 2), vc.UPLOADED)]


def test_stuck_device_thread_does_not_block_process_shutdown(monkeypatch):
    """멈춘 장치 호출은 데몬 스레드라 asyncio.run 정리(기본 실행기 종료 대기)에서 기다리지 않는다."""
    release = threading.Event()
    threading.Timer(5, release.set).start()  # 고치기 전 코드라도 테스트가 끝나도록

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(vc._run_in_thread(release.wait, 10), 0.1)

    started = time.monotonic()
    try:
        asyncio.run(go())
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# PiClient: 접속·종료 코드·재접속
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code, exit_code", [(4401, 3), (4403, 3), (4409, 4)])
def test_close_codes_that_stop_the_client(code, exit_code):
    async def go():
        connect = FakeConnect([FakeWS([hello(), closed(code, "x")])])
        client = PiClient(make_settings(), FakeFrameSource([]), FakeAudio(), connect=connect)
        with pytest.raises(ClientStop) as info:
            await client.run()
        return info.value, connect

    stop, connect = run(go())
    assert stop.code == exit_code
    assert str(code) in stop.message
    assert len(connect.calls) == 1  # 다시 접속하지 않는다


def test_connect_sends_key_header_and_tls_only_for_wss(tmp_path):
    async def go(settings):
        connect = FakeConnect([FakeWS([hello(), closed(4401)])])
        client = PiClient(settings, FakeFrameSource([]), FakeAudio(), connect=connect)
        with pytest.raises(ClientStop):
            await client.run()
        return connect.calls[0]

    url, kwargs, _ = run(go(make_settings()))
    assert url == "ws://pi-test.invalid/ws/camera?device_id=7"
    assert kwargs["additional_headers"] == {"X-Device-Key": KEY}
    assert "ssl" not in kwargs

    url, kwargs, _ = run(go(make_settings(server_url="https://pi-test.invalid:8443")))
    assert url == "wss://pi-test.invalid:8443/ws/camera?device_id=7"
    assert KEY not in url
    import ssl
    assert isinstance(kwargs["ssl"], ssl.SSLContext)
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_missing_ca_file_is_a_config_error(tmp_path):
    async def go():
        client = PiClient(make_settings(ca_file=str(tmp_path / "없음.pem")), FakeFrameSource([]), FakeAudio(),
                          connect=FakeConnect([]))
        with pytest.raises(ClientStop) as info:
            await client.run()
        return info.value
    assert run(go()).code == 2


def test_reconnects_with_backoff_and_resets_after_successful_connection():
    response = Response(502, "Bad Gateway", Headers())

    async def go():
        connect = FakeConnect([
            OSError("서버 꺼짐"),                 # 실패 1 -> 0.05초
            InvalidStatus(response),              # 실패 2 -> 0.3초
            FakeWS([closed(4400, "bad")]),        # hello 전에 4400 -> 실패 3 -> 0.3초(마지막 값 유지)
            FakeWS([hello(), closed(1011)]),      # 정상 접속 후 끊김 -> 간격 초기화 -> 0.05초
            FakeWS([hello(), closed(4409)]),      # 종료
        ])
        client = PiClient(make_settings(reconnect_delays=[0.05, 0.3]), FakeFrameSource([]), FakeAudio(),
                          connect=connect)
        with pytest.raises(ClientStop) as info:
            await client.run()
        return info.value, connect, client

    stop, connect, client = run(go())
    assert stop.code == 4
    times = [t for _, _, t in connect.calls]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(gaps) == 4
    assert 0.04 <= gaps[0] < 0.25
    assert gaps[1] >= 0.28 and gaps[2] >= 0.28
    assert gaps[3] < 0.25  # 정상 접속 뒤에는 처음 간격부터
    assert client.connections == 2


def test_stop_ends_run_and_cancels_running_voice_check():
    async def go():
        check = SlowVoiceCheck()
        ws = FakeWS([hello(), '{"type":"voice_check","event_id":3,"attempt":1}'])
        client = PiClient(make_settings(), FakeFrameSource([]), FakeAudio(), connect=FakeConnect([ws]),
                          voice_check=check)
        task = asyncio.create_task(client.run())
        await until(lambda: check.calls, what="음성 확인 시작")
        client.stop()
        await task  # 예외 없이 끝난다
        return check, client

    check, client = run(go())
    assert check.calls == [(3, 1)]
    assert check.cancelled == 1
    assert client.connected is False


def test_duplicate_voice_check_is_ignored_while_running():
    async def go():
        check = SlowVoiceCheck()
        ws = FakeWS([hello()])
        client = PiClient(make_settings(), FakeFrameSource([]), FakeAudio(), connect=FakeConnect([ws]),
                          voice_check=check)
        task = asyncio.create_task(client.run())
        await until(lambda: client.connected)

        ws.push('{"type":"state","state":"SUSPECT"}')
        ws.push('{"type":"voice_check","event_id":5,"attempt":1}')
        ws.push('{"type":"voice_check","event_id":5,"attempt":1}')  # 다시 접속했을 때처럼 같은 요청
        ws.push("not json")
        ws.push('{"type":"voice_check","event_id":"x"}')             # 잘못된 요청은 무시
        await until(lambda: client.state == "SUSPECT" and check.calls)
        await asyncio.sleep(0.1)
        assert check.calls == [(5, 1)]

        # 끝난 뒤 같은 요청이 다시 오면(서버가 업로드를 못 받음) 다시 한다.
        check.release.set()
        await until(lambda: client.voice_results)
        ws.push('{"type":"voice_check","event_id":5,"attempt":1}')
        await until(lambda: len(check.calls) == 2)
        await until(lambda: len(client.voice_results) == 2)
        client.stop()
        await task
        return client

    client = run(go())
    assert client.voice_results == [((5, 1), vc.UPLOADED), ((5, 1), vc.UPLOADED)]


def test_stale_queued_voice_check_is_skipped_for_newer_attempt():
    async def go():
        check = SlowVoiceCheck()
        ws = FakeWS([hello()])
        client = PiClient(make_settings(), FakeFrameSource([]), FakeAudio(), connect=FakeConnect([ws]),
                          voice_check=check)
        task = asyncio.create_task(client.run())
        await until(lambda: client.connected)
        ws.push('{"type":"voice_check","event_id":9,"attempt":1}')
        await until(lambda: check.calls)
        # 1회차가 끝나기 전에 2·3회차가 온다: 2회차는 기다리는 동안 낡았으므로 건너뛴다.
        ws.push('{"type":"voice_check","event_id":9,"attempt":2}')
        ws.push('{"type":"voice_check","event_id":9,"attempt":3}')
        await asyncio.sleep(0.05)
        check.release.set()
        await until(lambda: len(client.voice_results) == 2)
        client.stop()
        await task
        return check

    check = run(go())
    assert check.calls == [(9, 1), (9, 3)]


def test_frames_are_sent_latest_only_when_sending_is_slow():
    """전송이 느려도 밀린 프레임을 쌓지 않는다: 보내는 프레임은 항상 방금 찍은 것이다."""
    captured = []

    def capture():
        captured.append(time.monotonic())
        return str(captured[-1]).encode()

    async def go():
        ws = FakeWS([hello()], send_delay=0.15)
        client = PiClient(make_settings(fps=50.0), FakeFrameSource(capture), FakeAudio(),
                          connect=FakeConnect([ws]))
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.8)
        client.stop()
        await task
        return ws, client

    ws, client = run(go())
    assert 3 <= len(ws.sent) <= 7  # 50fps를 요청했지만 전송이 0.15초씩 걸리므로
    assert len(captured) <= len(ws.sent) + 1  # 보내지 못할 프레임은 찍지도 않는다
    for sent_at, data in ws.sent:
        assert sent_at - float(data) < 0.15 + 0.1  # 찍은 직후 보낸 프레임
    assert client.frames_sent == len(ws.sent)


def test_dead_camera_keeps_connection_without_frames():
    async def go():
        ws = FakeWS([hello()])
        frames = FakeFrameSource([])  # 항상 None
        client = PiClient(make_settings(fps=50.0), frames, FakeAudio(), connect=FakeConnect([ws]))
        task = asyncio.create_task(client.run())
        await until(lambda: client.connected)
        await asyncio.sleep(0.2)
        assert client.connected and not task.done()
        client.stop()
        await task
        return ws

    assert run(go()).sent == []


def test_frame_source_exception_does_not_drop_connection():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) % 2:
            raise RuntimeError("드라이버 오류")
        return b"jpeg"

    async def go():
        ws = FakeWS([hello()])
        client = PiClient(make_settings(fps=50.0), FakeFrameSource(flaky), FakeAudio(), connect=FakeConnect([ws]))
        task = asyncio.create_task(client.run())
        await until(lambda: len(ws.sent) >= 3)
        client.stop()
        await task
        return client

    assert run(go()).connections == 1


class StuckSendWS(FakeWS):
    """보낸 프레임이 상대에게 가지 않는 연결(버퍼가 꽉 참). send()가 끝나지 않는다."""

    async def send(self, data):
        await asyncio.Event().wait()


def test_stuck_frame_send_drops_connection_and_reconnects(monkeypatch):
    monkeypatch.setattr(client_mod, "SEND_TIMEOUT", 0.2)

    async def go():
        stuck = StuckSendWS([hello()])
        connect = FakeConnect([stuck, FakeWS([hello(), closed(4409)])])
        client = PiClient(make_settings(fps=50.0), FakeFrameSource([b"jpeg"]), FakeAudio(), connect=connect)
        with pytest.raises(ClientStop):
            await client.run()
        return stuck, connect, client

    stuck, connect, client = run(go())
    assert stuck.transport.aborted  # 막힌 연결은 닫기 인사 없이 끊는다
    assert len(connect.calls) == 2 and client.connections == 2


class StuckCloseWS(FakeWS):
    """닫기 인사도 보내지지 않는 연결. close()가 끝나지 않는다."""

    async def close(self):
        self.close_calls += 1
        await asyncio.Event().wait()


def test_stop_does_not_hang_when_close_is_stuck(monkeypatch):
    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT", 0.2)

    async def go():
        ws = StuckCloseWS([hello()])
        client = PiClient(make_settings(), FakeFrameSource([]), FakeAudio(), connect=FakeConnect([ws]))
        task = asyncio.create_task(client.run())
        await until(lambda: client.connected)
        client.stop()
        await asyncio.wait_for(task, 2)
        return ws

    ws = run(go())
    assert ws.close_calls == 1 and ws.transport.aborted


class DeafServer:
    """WebSocket 핸드셰이크와 hello까지만 하고 그 뒤로는 아무것도 읽지 않는 서버(반쯤 끊긴 연결 흉내).

    클라이언트가 보내는 프레임은 TCP 버퍼에 쌓이다가 꽉 차고, 그 뒤로 send()·ping·close()가 모두 막힌다.
    """

    GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # 받은 연결도 이 크기를 물려받는다
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.held = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        import base64
        import hashlib
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1(key + self.GUID).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            message = hello(7).encode()
            conn.sendall(bytes([0x81, len(message)]) + message)
            self.held.append(conn)  # 닫지도 읽지도 않는다

    def close(self):
        self._stop = True
        self._thread.join(2)
        self.sock.close()
        for conn in self.held:
            conn.close()


@pytest.fixture
def deaf_server():
    server = DeafServer()
    yield server
    server.close()


def test_half_open_connection_while_streaming_reconnects(deaf_server, monkeypatch):
    """실제 websockets 연결: 상대가 읽지 않아 버퍼가 차면 전송 시간 제한으로 끊고 다시 접속한다.

    ping은 막힌 버퍼 비우기에서 같이 멈추므로 이 경우를 잡지 못한다(PING_*을 그대로 두고 확인한다).
    """
    monkeypatch.setattr(client_mod, "SEND_TIMEOUT", 0.5)
    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT", 0.5)

    async def go():
        settings = make_settings(server_url=f"http://127.0.0.1:{deaf_server.port}", fps=50.0)
        client = PiClient(settings, FakeFrameSource([b"x" * 256_000]), FakeAudio())
        task = asyncio.create_task(client.run())
        try:
            await until(lambda: client.connections >= 2, timeout=8, what="다시 접속")
        finally:
            client.stop()
            await asyncio.wait_for(task, 3)
        return client

    client = run(go(), timeout=15)
    assert client.frames_sent > 0


def test_stop_with_stuck_real_connection_finishes_quickly(deaf_server, monkeypatch):
    """systemctl stop: 버퍼가 막힌 연결에서도 close()에 시간 제한이 있어 run()이 바로 끝난다."""
    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT", 0.5)

    async def go():
        settings = make_settings(server_url=f"http://127.0.0.1:{deaf_server.port}", fps=50.0)
        client = PiClient(settings, FakeFrameSource([b"x" * 256_000]), FakeAudio())
        task = asyncio.create_task(client.run())
        await until(lambda: client.connected, what="접속")
        # 프레임 수가 더 늘지 않을 때까지(버퍼가 꽉 참) 기다린다.
        last, still_since = -1, time.monotonic()
        while time.monotonic() - still_since < 0.5:
            if client.frames_sent != last:
                last, still_since = client.frames_sent, time.monotonic()
            await asyncio.sleep(0.05)
        started = time.monotonic()
        client.stop()
        await asyncio.wait_for(task, 3)
        return time.monotonic() - started

    assert run(go(), timeout=15) < 2.0


def test_retrying_frame_source_opens_camera_later():
    now = [0.0]
    attempts = []

    class Ok(FrameSource):
        name = "ok"
        closed = False

        def next_jpeg(self):
            return b"jpeg"

        def close(self):
            Ok.closed = True

    def factory():
        attempts.append(now[0])
        if len(attempts) < 3:
            raise CameraError("카메라 없음")
        return Ok()

    source = RetryingFrameSource(factory, interval=5.0, clock=lambda: now[0])
    assert source.next_jpeg() is None       # 1번째 시도 실패
    now[0] = 2.0
    assert source.next_jpeg() is None       # 간격 전: 시도하지 않음
    now[0] = 5.0
    assert source.next_jpeg() is None       # 2번째 시도 실패
    now[0] = 10.0
    assert source.next_jpeg() == b"jpeg"    # 3번째 시도 성공
    assert source.next_jpeg() == b"jpeg"
    assert attempts == [0.0, 5.0, 10.0]
    source.close()
    assert Ok.closed


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------

PI_VARS = ["PI_SERVER_URL", "PI_DEVICE_ID", "PI_DEVICE_KEY", "PI_CA_FILE", "PI_CAMERA", "PI_CAMERA_INDEX",
           "PI_VIDEO_PATH", "PI_FPS", "PI_AUDIO", "PI_PROMPT_WAV", "PI_RECORD_SECONDS", "PI_INPUT_DEVICE",
           "PI_OUTPUT_DEVICE"]


@pytest.fixture
def pi_env(monkeypatch):
    for name in PI_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PI_SERVER_URL", "http://pi-test.invalid")
    monkeypatch.setenv("PI_DEVICE_ID", "7")
    monkeypatch.setenv("PI_DEVICE_KEY", KEY)
    monkeypatch.setenv("PI_CAMERA", "fake")
    monkeypatch.setenv("PI_AUDIO", "fake")
    return monkeypatch


@pytest.mark.parametrize("name, value", [
    ("PI_DEVICE_ID", "0"),
    ("PI_DEVICE_KEY", ""),
    ("PI_FPS", ""),           # 빈 숫자 값
    ("PI_FPS", "0"),
    ("PI_CAMERA", "webcam"),
    ("PI_AUDIO", "alsa"),
    ("PI_SERVER_URL", "192.168.0.10:8443"),
])
def test_main_exits_2_on_bad_config(pi_env, name, value):
    pi_env.setenv(name, value)
    assert main_mod.main() == 2


def test_main_maps_client_stop_to_exit_code(pi_env, monkeypatch):
    seen = {}

    async def fake_run(self):
        seen["source"] = self.frame_source.name
        raise ClientStop(4, "교체됨")

    monkeypatch.setattr(PiClient, "run", fake_run)
    assert main_mod.main() == 4
    assert seen["source"] == "fake"

    async def stopped(self):
        return None

    monkeypatch.setattr(PiClient, "run", stopped)
    assert main_mod.main() == 0


def test_main_keeps_running_without_camera(pi_env, monkeypatch):
    pi_env.setenv("PI_CAMERA", "opencv")

    def no_camera(settings, fake_frames=None):
        raise CameraError("카메라 없음")

    monkeypatch.setattr(main_mod, "create_frame_source", no_camera)
    seen = {}

    async def fake_run(self):
        seen["source"] = self.frame_source
        return None

    monkeypatch.setattr(PiClient, "run", fake_run)
    assert main_mod.main() == 0
    assert isinstance(seen["source"], RetryingFrameSource)


def test_exit_codes_match_systemd_unit():
    unit = (Path(main_mod.__file__).parent / "deploy" / "pi-client.service").read_text(encoding="utf-8")
    assert "RestartPreventExitStatus=2 3 4" in unit
    assert (client_mod.EXIT_CONFIG, client_mod.EXIT_AUTH, client_mod.EXIT_REPLACED) == (2, 3, 4)


# ---------------------------------------------------------------------------
# 끝까지: 실제 서버에 붙이기
# ---------------------------------------------------------------------------

@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """가짜 자세 추정·가짜 음성 인식으로 실제 서버를 띄운다. (기본 주소, 서버 설정)"""
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("server.main", reason="중앙 서버 코드가 없어 건너뜀 (FALL_SERVER_REPO=<메인 저장소 경로>)")
    from server import auth
    from server.config import Settings
    from server.main import create_app

    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)
    settings = Settings(
        data_dir=tmp_path / "data",
        web_dir=tmp_path / "web",
        cookie_secure=False,
        pose_backend="fake",
        stt_backend="fake",
        t1_lying_seconds=0.3,
        voice_max_attempts=2,
        voice_reply_timeout_seconds=6.0,
        camera_stale_seconds=1.0,
        vapid_private_key=None,
        vapid_public_key=None,
        push_retry_delays=[],
        device_reconnect_grace_seconds=0.2,
    )
    app = create_app(settings)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail("테스트 서버가 시작되지 않음")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", settings
    server.should_exit = True
    thread.join(10)


async def register(base_url, email="grandma@example.com"):
    """사용자와 Pi 기기를 만든다. (로그인된 httpx 클라이언트, 기기 ID, 기기 키)"""
    user = httpx.AsyncClient(base_url=base_url, timeout=5.0)
    res = await user.post("/api/signup", json={"email": email, "password": "password123", "name": "할머니"})
    assert res.status_code == 201, res.text
    res = await user.post("/api/devices", json={"name": "거실 Pi", "kind": "pi"})
    assert res.status_code == 201, res.text
    body = res.json()
    return user, body["device"]["id"], body["key"]


def pi_settings(base_url, device_id, key, prompt_wav):
    return make_settings(server_url=base_url, device_id=device_id, device_key=key, fps=20.0,
                         reconnect_delays=[0.1, 0.2], prompt_wav=prompt_wav, record_seconds=0.1)


def fall_frames():
    """서 있다가(5장) 엉덩이가 몸통 길이 2배만큼 떨어져 누운 뒤 계속 누워 있는 영상."""
    from server.detection.pose import fake_frame
    return FakeFrameSource([fake_frame("standing")] * 5 + [fake_frame("lying", offset_y=100)])


def standing_frames():
    from server.detection.pose import fake_frame
    return FakeFrameSource([fake_frame("standing")])


async def wait_event(user, predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while True:
        res = await user.get("/api/events")
        assert res.status_code == 200, res.text
        for event in res.json()["events"]:
            if predicate(event):
                return event
        if time.monotonic() > deadline:
            raise AssertionError(f"기다리던 이벤트가 없음: {res.json()['events']}")
        await asyncio.sleep(0.05)


async def stop_client(client, task):
    client.stop()
    await asyncio.wait_for(task, 5)


def test_e2e_voice_reply_safe_clears_event(live_server, prompt):
    from server.db import CLEARED_VOICE
    base_url, _ = live_server

    async def go():
        user, device_id, key = await register(base_url)
        audio = FakeAudio(["네 괜찮아요"])
        client = PiClient(pi_settings(base_url, device_id, key, prompt), fall_frames(), audio)
        task = asyncio.create_task(client.run())
        try:
            event = await wait_event(user, lambda e: e["result"] == CLEARED_VOICE)
            await until(lambda: client.state == "NORMAL", what="NORMAL 상태")
        finally:
            await stop_client(client, task)
            await user.aclose()
        return event, client, audio

    event, client, audio = run(go(), timeout=15)
    assert event["state"] == "ENDED" and event["voice_result"] == "SAFE"
    assert client.voice_results == [((event["id"], 1), vc.UPLOADED)]
    assert audio.played == [prompt] and len(audio.recorded) == 1
    assert client.frames_sent > 5


def test_e2e_mic_failure_raises_alarm_without_waiting_for_timeout(live_server, prompt):
    from server.db import ALERT_VOICE_FAIL
    base_url, server_settings = live_server

    async def go():
        user, device_id, key = await register(base_url)
        client = PiClient(pi_settings(base_url, device_id, key, prompt), fall_frames(), FakeAudio([None]))
        task = asyncio.create_task(client.run())
        try:
            await until(lambda: client.voice_results, timeout=5, what="음성 확인")
            reported = time.monotonic()
            event = await wait_event(user, lambda e: e["result"] == ALERT_VOICE_FAIL, timeout=3)
            waited = time.monotonic() - reported
            await until(lambda: client.state == "ALARM", what="ALARM 상태")
        finally:
            await stop_client(client, task)
            await user.aclose()
        return event, client, waited

    event, client, waited = run(go(), timeout=15)
    assert event["state"] == "ALARM"
    assert client.voice_results == [((event["id"], 1), vc.REPORTED_ERROR)]
    assert waited < server_settings.voice_reply_timeout_seconds / 2  # 응답 대기 시간을 기다리지 않았다


def test_e2e_second_client_replaces_first_with_4409(live_server, prompt):
    base_url, _ = live_server

    async def go():
        user, device_id, key = await register(base_url)
        settings = pi_settings(base_url, device_id, key, prompt)
        first = PiClient(settings, standing_frames(), FakeAudio())
        first_task = asyncio.create_task(first.run())
        await until(lambda: first.connected, what="첫 클라이언트 접속")
        second = PiClient(settings, standing_frames(), FakeAudio())
        second_task = asyncio.create_task(second.run())
        try:
            with pytest.raises(ClientStop) as info:
                await asyncio.wait_for(first_task, 5)
            await until(lambda: second.connected, what="두 번째 클라이언트 접속")
            res = await user.get(f"/api/devices/{device_id}/state")
            assert res.status_code == 200 and res.json()["device"]["online"] is True
        finally:
            await stop_client(second, second_task)
            await user.aclose()
        return info.value, second

    stop, second = run(go(), timeout=15)
    assert stop.code == 4 and "4409" in stop.message
    assert second.connections == 1  # 두 번째 클라이언트는 교체당하지 않았다


def test_e2e_wrong_key_stops_with_exit_code_3(live_server, prompt):
    base_url, _ = live_server

    async def go():
        user, device_id, key = await register(base_url)
        await user.aclose()
        results = []
        for bad_id, bad_key in ((device_id, "wrong-key"), (device_id + 100, key)):
            client = PiClient(pi_settings(base_url, bad_id, bad_key, prompt), standing_frames(), FakeAudio())
            with pytest.raises(ClientStop) as info:
                await asyncio.wait_for(client.run(), 5)
            results.append((info.value, client))
        return results

    for stop, client in run(go(), timeout=15):
        assert stop.code == 3 and "4401" in stop.message
        assert client.connections == 0
