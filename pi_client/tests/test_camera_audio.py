"""카메라·오디오 장치 계층 테스트. 실제 하드웨어 없이 가짜 장치와 임시 파일로 확인한다."""

import io
import json
import sys
import types
import wave
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from pi_client import audio as audio_mod
from pi_client import camera as camera_mod
from pi_client.audio import (AudioError, FakeAudio, SoundDeviceAudio, create_audio, peak_level,
                             wav_bytes)
from pi_client.camera import (Camera, CameraError, CameraFrameSource, FakeFrameSource, OpenCVCamera,
                              VideoFileCamera, create_frame_source, encode_jpeg)
from pi_client.config import ClientSettings


def make_settings(**overrides) -> ClientSettings:
    s = ClientSettings()
    s.camera_backend = "fake"
    s.audio_backend = "fake"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def decode(jpeg: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


# ---------------------------------------------------------------------------
# 카메라
# ---------------------------------------------------------------------------

def test_encode_jpeg_resizes_to_target():
    frame = np.full((240, 320, 3), 128, dtype=np.uint8)
    data = encode_jpeg(frame, 70, 640, 480)
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    assert decode(data).shape == (480, 640, 3)


def test_encode_jpeg_keeps_size_without_target_and_quality_matters():
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    low = encode_jpeg(frame, 10)
    high = encode_jpeg(frame, 95)
    assert decode(low).shape == (120, 160, 3)
    assert len(low) < len(high)


def test_encode_jpeg_rejects_empty():
    with pytest.raises(CameraError):
        encode_jpeg(np.zeros((0, 0, 3), dtype=np.uint8), 70)


@pytest.fixture
def tiny_video(tmp_path):
    """프레임 3장짜리 영상. 이 환경에 코덱이 없으면 건너뛴다."""
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    if not writer.isOpened():
        pytest.skip("MJPG 코덱을 쓸 수 없음")
    for value in (0, 120, 240):
        writer.write(np.full((48, 64, 3), value, dtype=np.uint8))
    writer.release()
    cap = cv2.VideoCapture(str(path))
    ok, _ = cap.read()
    cap.release()
    if not ok:
        pytest.skip("만든 영상을 다시 읽을 수 없음")
    return path


def test_video_file_camera_loops(tiny_video):
    cam = VideoFileCamera(tiny_video)
    cam.open()
    try:
        means = []
        for _ in range(7):
            frame = cam.read()
            assert frame is not None
            means.append(int(frame.mean()))
    finally:
        cam.close()
    # 3장을 다 읽은 뒤 처음(어두운 프레임)으로 돌아간다.
    assert means[0] < 30 and means[3] < 30 and means[6] < 30
    assert means[2] > 200 and means[5] > 200


def test_video_file_camera_missing_file(tmp_path):
    with pytest.raises(CameraError, match="PI_VIDEO_PATH"):
        VideoFileCamera(tmp_path / "nope.mp4").open()


def test_create_frame_source_file_backend(tiny_video):
    source = create_frame_source(make_settings(camera_backend="file", video_path=str(tiny_video)))
    try:
        assert isinstance(source, CameraFrameSource)
        # 영상은 64x48이지만 설정 크기(640x480)로 맞춰 보낸다.
        assert decode(source.next_jpeg()).shape == (480, 640, 3)
    finally:
        source.close()


def test_create_frame_source_file_requires_path():
    with pytest.raises(CameraError, match="PI_VIDEO_PATH"):
        create_frame_source(make_settings(camera_backend="file", video_path=None))


def test_fake_frame_source_list_repeats_last():
    source = FakeFrameSource([b"a", b"b", b"c"])
    assert [source.next_jpeg() for _ in range(5)] == [b"a", b"b", b"c", b"c", b"c"]
    assert source.sent == 5


def test_fake_frame_source_loop_and_callable_and_empty():
    looped = FakeFrameSource([b"a", b"b"], loop=True)
    assert [looped.next_jpeg() for _ in range(5)] == [b"a", b"b", b"a", b"b", b"a"]

    values = iter([b"x", None, b"y"])
    func = FakeFrameSource(lambda: next(values))
    assert [func.next_jpeg() for _ in range(3)] == [b"x", None, b"y"]
    assert func.sent == 2

    assert FakeFrameSource([]).next_jpeg() is None


def test_create_frame_source_fake_default_matches_server_standing_frame():
    source = create_frame_source(make_settings())
    assert isinstance(source, FakeFrameSource)
    data = source.next_jpeg()
    try:
        from server.detection.pose import fake_frame
    except Exception:  # 서버 의존성이 없는 환경(Pi)에서는 모양만 확인
        assert set(json.loads(data)) == {"ls", "rs", "lh", "rh", "bbox"}
    else:
        assert json.loads(data) == json.loads(fake_frame("standing"))


def test_create_frame_source_fake_uses_given_frames():
    source = create_frame_source(make_settings(), fake_frames=[b"one"])
    assert source.next_jpeg() == b"one"


def test_create_frame_source_unknown_backend():
    with pytest.raises(CameraError, match="알 수 없는 카메라"):
        create_frame_source(make_settings(camera_backend="kinect"))


class FailingCamera(Camera):
    name = "broken"

    def __init__(self, message):
        self.message = message

    def open(self):
        raise CameraError(self.message)

    def read(self):
        return None


def test_create_frame_source_auto_falls_back_to_opencv(monkeypatch):
    opened = []

    class GoodCamera(Camera):
        name = "opencv"

        def __init__(self, *args):
            pass

        def open(self):
            opened.append(self.name)

        def read(self):
            return np.zeros((480, 640, 3), dtype=np.uint8)

    monkeypatch.setattr(camera_mod, "Picamera2Camera", lambda w, h: FailingCamera("모듈 없음"))
    monkeypatch.setattr(camera_mod, "OpenCVCamera", GoodCamera)
    source = create_frame_source(make_settings(camera_backend="auto"))
    assert opened == ["opencv"]
    assert source.name == "opencv"
    assert source.next_jpeg()[:2] == b"\xff\xd8"


def test_create_frame_source_auto_nothing_opens(monkeypatch):
    monkeypatch.setattr(camera_mod, "Picamera2Camera", lambda w, h: FailingCamera("모듈 없음"))
    monkeypatch.setattr(camera_mod, "OpenCVCamera", lambda i, w, h: FailingCamera("웹캠 없음"))
    with pytest.raises(CameraError) as info:
        create_frame_source(make_settings(camera_backend="auto"))
    text = str(info.value)
    assert "카메라를 찾지 못했습니다" in text and "모듈 없음" in text and "웹캠 없음" in text
    assert "PI_CAMERA=fake" in text


def test_picamera2_missing_gives_install_hint(monkeypatch):
    # picamera2가 설치된 환경이어도 import가 실패하도록 막는다.
    monkeypatch.setitem(sys.modules, "picamera2", None)
    with pytest.raises(CameraError, match="python3-picamera2"):
        create_frame_source(make_settings(camera_backend="picamera2"))


def test_picamera2_broken_import_falls_back_to_opencv(monkeypatch, tmp_path):
    """numpy ABI 불일치처럼 ImportError가 아닌 예외로 picamera2 import가 실패해도 프로그램이 죽지 않는다."""
    (tmp_path / "picamera2.py").write_text("raise ValueError('numpy.dtype size changed')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "picamera2", raising=False)

    with pytest.raises(CameraError, match="numpy"):
        create_frame_source(make_settings(camera_backend="picamera2"))

    class GoodCamera(Camera):
        name = "opencv"

        def __init__(self, *args):
            pass

        def open(self):
            pass

        def read(self):
            return np.zeros((480, 640, 3), dtype=np.uint8)

    monkeypatch.delitem(sys.modules, "picamera2", raising=False)
    monkeypatch.setattr(camera_mod, "OpenCVCamera", GoodCamera)
    source = create_frame_source(make_settings(camera_backend="auto"))
    assert source.name == "opencv"


def test_picamera2_camera_with_fake_module(monkeypatch):
    calls = []

    class FakePicamera2:
        def create_video_configuration(self, main):
            calls.append(("config", main))
            return main

        def configure(self, config):
            calls.append("configure")

        def start(self):
            calls.append("start")

        def capture_array(self):
            return np.zeros((480, 640, 4), dtype=np.uint8)  # 4채널로 와도 BGR 3채널로 바꾼다

        def stop(self):
            calls.append("stop")

        def close(self):
            calls.append("close")

    monkeypatch.setitem(sys.modules, "picamera2", types.SimpleNamespace(Picamera2=FakePicamera2))
    cam = camera_mod.Picamera2Camera(640, 480)
    cam.open()
    assert cam.read().shape == (480, 640, 3)
    cam.close()
    assert calls[0] == ("config", {"size": (640, 480), "format": "RGB888"})
    assert calls[-2:] == ["stop", "close"]


def test_opencv_camera_unavailable_index():
    # 존재하지 않는 장치 번호는 열리지 않는다.
    with pytest.raises(CameraError):
        OpenCVCamera(99, 640, 480).open()


class FlakyCamera(Camera):
    """reads 목록대로 프레임/None을 내고, open_results대로 열기에 성공/실패한다."""
    name = "flaky"

    def __init__(self, reads, open_results=None):
        self.reads = list(reads)
        self.open_results = list(open_results or [])
        self.open_calls = 0
        self.close_calls = 0

    def open(self):
        self.open_calls += 1
        if self.open_results and not self.open_results.pop(0):
            raise CameraError("아직 연결 안 됨")

    def read(self):
        return self.reads.pop(0) if self.reads else None

    def close(self):
        self.close_calls += 1


def test_camera_frame_source_reopens_after_repeated_failures():
    now = [0.0]
    good = np.zeros((48, 64, 3), dtype=np.uint8)
    cam = FlakyCamera([None, None, None, good], open_results=[True, False, True])
    source = CameraFrameSource(cam, 70, 64, 48, max_failures=3, reopen_interval=3.0, clock=lambda: now[0])
    source.open()
    assert [source.next_jpeg() for _ in range(3)] == [None, None, None]
    assert not source.is_open and cam.close_calls == 1

    # 다시 열기 간격 전에는 장치를 건드리지 않는다.
    now[0] = 1.0
    assert source.next_jpeg() is None and cam.open_calls == 1
    # 첫 재시도는 실패, 다음 간격에 성공하면 다시 프레임이 나온다.
    now[0] = 3.5
    assert source.next_jpeg() is None and cam.open_calls == 2
    now[0] = 7.0
    data = source.next_jpeg()
    assert cam.open_calls == 3 and source.is_open
    assert decode(data).shape == (48, 64, 3)


def test_camera_frame_source_swallows_read_exceptions():
    class ExplodingCamera(Camera):
        name = "boom"

        def open(self):
            pass

        def read(self):
            raise RuntimeError("드라이버 오류")

    source = CameraFrameSource(ExplodingCamera(), 70, 64, 48)
    source.open()
    assert source.next_jpeg() is None


# ---------------------------------------------------------------------------
# 오디오
# ---------------------------------------------------------------------------

def test_wav_bytes_round_trip():
    samples = (np.sin(np.linspace(0, 20, 1600)) * 10000).astype(np.int16)
    data = wav_bytes(samples.reshape(-1, 1), 16000)  # sd.rec 모양 (N, 1)도 받는다
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        back = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert np.array_equal(back, samples)
    assert peak_level(data) == pytest.approx(np.abs(samples).max() / 32768.0)


def test_wav_bytes_rejects_wrong_input():
    with pytest.raises(ValueError):
        wav_bytes(np.zeros(10, dtype=np.float32), 16000)
    with pytest.raises(ValueError):
        wav_bytes(np.zeros((10, 2), dtype=np.int16), 16000)


def test_fake_audio_behaviour():
    fake = FakeAudio(["괜찮아요", None])
    fake.play_wav("/no/such/prompt.wav")
    assert [str(p) for p in fake.played] == ["/no/such/prompt.wav"]
    assert fake.record(5, 16000) == "괜찮아요".encode("utf-8")
    with pytest.raises(AudioError):
        fake.record(5, 16000)
    assert fake.record(5, 16000) == b""
    assert fake.recorded == [5, 5, 5]

    with pytest.raises(AudioError):
        FakeAudio([], play_error=True).play_wav("x.wav")


def test_create_audio_selection(monkeypatch):
    fake = create_audio(make_settings(), fake_replies=["살려줘"])
    assert isinstance(fake, FakeAudio) and fake.record(1, 16000) == "살려줘".encode()

    def no_sd():
        raise AudioError("sounddevice 사용 불가: PortAudio library not found")

    monkeypatch.setattr(audio_mod, "_import_sounddevice", no_sd)
    real = create_audio(make_settings(audio_backend="sounddevice", input_device="2", output_device="USB Audio"))
    # PortAudio가 없어도 만들어지고(경고만), 녹음할 때 AudioError로 알린다.
    assert isinstance(real, SoundDeviceAudio)
    assert real.input_device == 2 and real.output_device == "USB Audio"
    with pytest.raises(AudioError, match="sounddevice"):
        real.record(1, 16000)

    with pytest.raises(AudioError, match="알 수 없는 오디오"):
        create_audio(make_settings(audio_backend="bluetooth"))


class FakeSD:
    """sounddevice 대역."""

    def __init__(self, play_error=None, rec_error=None):
        self.play_error = play_error
        self.rec_error = rec_error
        self.played = []
        self.rec_args = None

    def play(self, data, samplerate, device, blocking):
        if self.play_error:
            raise self.play_error
        self.played.append((data.copy(), samplerate, device, blocking))

    def rec(self, frames, samplerate, channels, dtype, device):
        if self.rec_error:
            raise self.rec_error
        self.rec_args = (frames, samplerate, channels, dtype, device)
        out = np.zeros((frames, channels), dtype=np.int16)
        out[0, 0] = 16384
        return out

    def wait(self):
        pass


def write_prompt(path, samples=None, rate=16000):
    samples = np.arange(-50, 50, dtype=np.int16) if samples is None else samples
    path.write_bytes(wav_bytes(samples, rate))
    return samples


def test_sounddevice_audio_play_and_record(monkeypatch, tmp_path):
    sd = FakeSD()
    monkeypatch.setattr(audio_mod, "_import_sounddevice", lambda: sd)
    prompt = tmp_path / "ask.wav"
    samples = write_prompt(prompt)

    dev = SoundDeviceAudio(input_device="1", output_device="3")
    dev.play_wav(prompt)
    data, rate, device, blocking = sd.played[0]
    assert np.array_equal(data[:, 0], samples) and rate == 16000 and device == 3 and blocking is True

    wav = dev.record(0.5, 16000)
    assert sd.rec_args == (8000, 16000, 1, "int16", 1)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnframes() == 8000 and w.getframerate() == 16000
    assert peak_level(wav) == pytest.approx(0.5)


def test_sounddevice_audio_errors(monkeypatch, tmp_path):
    sd = FakeSD(rec_error=RuntimeError("Error querying device -1"))
    monkeypatch.setattr(audio_mod, "_import_sounddevice", lambda: sd)
    dev = SoundDeviceAudio()
    with pytest.raises(AudioError, match="마이크 녹음 실패"):
        dev.record(1, 16000)
    with pytest.raises(AudioError):
        dev.record(0, 16000)
    with pytest.raises(AudioError, match="안내 음성 파일 없음"):
        dev.play_wav(tmp_path / "missing.wav")


def test_sounddevice_playback_falls_back_to_aplay(monkeypatch, tmp_path):
    sd = FakeSD(play_error=RuntimeError("Invalid sample rate"))
    monkeypatch.setattr(audio_mod, "_import_sounddevice", lambda: sd)
    prompt = tmp_path / "ask.wav"
    write_prompt(prompt)
    runs = []
    monkeypatch.setattr(audio_mod.subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    SoundDeviceAudio().play_wav(prompt)
    assert runs == [["aplay", "-q", str(prompt)]]

    def no_aplay(cmd, **kw):
        raise FileNotFoundError("aplay")

    monkeypatch.setattr(audio_mod.subprocess, "run", no_aplay)
    with pytest.raises(AudioError, match="alsa-utils"):
        SoundDeviceAudio().play_wav(prompt)


class ReplugSD(FakeSD):
    """장치 목록이 낡은 sounddevice: PortAudio를 다시 초기화하기 전까지는 장치를 찾지 못한다."""

    def __init__(self):
        super().__init__()
        self.refreshed = 0
        self.terminated = 0

    def _terminate(self):
        self.terminated += 1

    def _initialize(self):
        self.refreshed += 1

    def rec(self, frames, samplerate, channels, dtype, device):
        if not self.refreshed:
            raise RuntimeError("No input device matching 'USB PnP'")
        return super().rec(frames, samplerate, channels, dtype, device)

    def play(self, data, samplerate, device, blocking):
        if not self.refreshed:
            raise RuntimeError("Error querying device -1")
        super().play(data, samplerate, device, blocking)


def test_replugged_device_is_found_after_refreshing_portaudio(monkeypatch, tmp_path):
    """마이크를 다시 꽂았거나 늦게 잡힌 경우: 장치 목록을 새로 읽고 한 번 더 해서 성공한다."""
    sd = ReplugSD()
    monkeypatch.setattr(audio_mod, "_import_sounddevice", lambda: sd)
    dev = SoundDeviceAudio(input_device="USB PnP")
    wav = dev.record(0.1, 16000)
    assert sd.terminated == 1 and sd.refreshed == 1
    assert sd.rec_args == (1600, 16000, 1, "int16", "USB PnP")
    assert peak_level(wav) == pytest.approx(0.5)

    sd = ReplugSD()
    monkeypatch.setattr(audio_mod, "_import_sounddevice", lambda: sd)
    prompt = tmp_path / "ask.wav"
    write_prompt(prompt)
    monkeypatch.setattr(audio_mod.subprocess, "run", lambda cmd, **kw: pytest.fail("aplay까지 가지 않는다"))
    SoundDeviceAudio().play_wav(prompt)
    assert sd.refreshed == 1 and len(sd.played) == 1


def test_sounddevice_import_opens_alsa_hw_devices_through_plughw(monkeypatch, tmp_path):
    """16 kHz를 못 받는 USB 마이크도 쓰도록 PortAudio 초기화 전에 PA_ALSA_PLUGHW=1을 넣는다."""
    (tmp_path / "sounddevice.py").write_text("import os\nPLUGHW_AT_IMPORT = os.environ.get('PA_ALSA_PLUGHW')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.delenv("PA_ALSA_PLUGHW", raising=False)
    sd = audio_mod._import_sounddevice()
    assert sd.PLUGHW_AT_IMPORT == "1"

    # 직접 정한 값은 바꾸지 않는다.
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setenv("PA_ALSA_PLUGHW", "0")
    assert audio_mod._import_sounddevice().PLUGHW_AT_IMPORT == "0"


def test_self_test_writes_file(monkeypatch, tmp_path, capsys):
    sd = FakeSD()
    monkeypatch.setattr(audio_mod, "_import_sounddevice", lambda: sd)
    monkeypatch.setenv("PI_PROMPT_WAV", str(tmp_path / "missing.wav"))
    out = tmp_path / "rec.wav"
    assert audio_mod._self_test(0.2, out) == 0
    assert out.is_file()
    printed = capsys.readouterr().out
    assert "재생 실패" in printed and "최대 음량: 50%" in printed


def test_device_secrets_and_prompt_audio_are_git_ignored():
    """README가 저장소 안에 두라고 하는 기기 키·인증 기관·안내 음성은 실수로 커밋되지 않아야 한다."""
    import shutil
    import subprocess
    from pathlib import Path

    if shutil.which("git") is None:
        pytest.skip("git 없음")
    repo = Path(__file__).resolve().parents[2]
    paths = ["pi_client/deploy/pi-client.env", "pi_client/deploy/rootCA.pem", "pi_client/assets/ask_status.wav"]
    res = subprocess.run(["git", "check-ignore", *paths], cwd=repo, capture_output=True, text=True)
    if res.returncode == 128:
        pytest.skip(f"git 저장소가 아님: {res.stderr.strip()}")
    assert res.stdout.split() == paths
    # 예시 파일은 저장소에 있어야 한다.
    res = subprocess.run(["git", "check-ignore", "pi_client/deploy/pi-client.env.example", "pi_client/assets/README.md"],
                         cwd=repo, capture_output=True, text=True)
    assert res.stdout.strip() == ""
