"""스피커·마이크. 음성 확인 때 안내 음성을 틀고 응답을 녹음해 WAV 바이트로 돌려준다.

- SoundDeviceAudio: 실제 장치(sounddevice/PortAudio). 재생이 안 되면 aplay로 한 번 더 시도한다.
- FakeAudio: 테스트용. 녹음 대신 정해 둔 문장을 UTF-8 바이트로 돌려준다(서버의 가짜 음성 인식기가 그대로 읽는다).

play_wav()/record()는 블로킹 함수이므로 이벤트 루프에서는 스레드에서 호출한다
(voice_check.py가 시간 제한을 두고 데몬 스레드에서 부른다).
실패는 모두 AudioError로 알린다. 호출하는 쪽은 녹음 실패를 서버에 'error'로 보고해
서버가 응답 시간 초과를 기다리지 않고 바로 '확인 불가' 알림을 낼 수 있게 한다.

마이크 감도 맞추기: python -m pi_client.audio --test
"""

import io
import logging
import os
import subprocess
import threading
import wave
from pathlib import Path
from typing import List, Optional, Union

import numpy as np

log = logging.getLogger("pi.audio")


class AudioError(RuntimeError):
    """재생·녹음 실패. 메시지는 서버의 'error' 필드로 그대로 보낼 수 있을 만큼 짧게."""


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """16비트 모노 샘플(int16)을 WAV 파일 바이트로 만든다(디스크에 쓰지 않는다)."""
    arr = np.asarray(samples)
    if arr.dtype != np.int16:
        raise ValueError(f"int16 샘플이어야 합니다 (받은 형식: {arr.dtype})")
    if arr.ndim == 2 and arr.shape[1] == 1:  # sd.rec은 (프레임 수, 채널 수) 모양으로 준다
        arr = arr[:, 0]
    if arr.ndim != 1:
        raise ValueError(f"모노 샘플이어야 합니다 (받은 모양: {arr.shape})")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(arr.astype("<i2", copy=False).tobytes())
    return buf.getvalue()


def peak_level(wav: bytes) -> float:
    """16비트 WAV의 최대 음량을 0.0~1.0(최대치 대비)으로 돌려준다. 마이크 감도 확인용."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("16비트 WAV만 지원합니다")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if data.size == 0:
        return 0.0
    return float(np.max(np.abs(data.astype(np.int32)))) / 32768.0


def _parse_device(value: Optional[Union[str, int]]) -> Optional[Union[str, int]]:
    """장치 지정값. 숫자만 있으면 장치 번호(int), 아니면 이름 일부(str), 비어 있으면 기본 장치(None)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = value.strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _import_sounddevice():
    """sounddevice를 쓸 때만 불러온다. PortAudio(libportaudio2)가 없으면 import에서 OSError가 난다."""
    # PortAudio는 ALSA 장치를 날것(hw:N,0)으로 열어 표본율을 바꿔 주지 않는다. USB 마이크는 대개
    # 44.1/48 kHz만 되므로 16 kHz 녹음이 'Invalid sample rate'로 실패한다. plughw로 열면 ALSA가 바꿔 준다.
    # PortAudio를 초기화할 때(import, _refresh_devices) 읽으므로 그 전에 넣는다. 직접 정한 값이 있으면 그대로 둔다.
    os.environ.setdefault("PA_ALSA_PLUGHW", "1")
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:
        raise AudioError(f"sounddevice 사용 불가: {e}") from e
    return sd


def _refresh_devices(sd) -> None:
    """PortAudio를 다시 초기화해 장치 목록을 새로 읽는다.

    PortAudio는 장치 목록을 초기화할 때 한 번만 만든다. USB 마이크를 뽑았다 꽂았거나, 서비스가
    PipeWire(사용자 세션)보다 먼저 떠서 기본 장치가 빠진 채 목록이 만들어졌으면 다시 읽어야 보인다.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:  # 다시 초기화하지 못해도 다음 시도에서 같은 오류로 알린다
        log.warning("오디오 장치 목록을 다시 읽지 못했습니다: %s", e)


def _with_device_retry(sd, what: str, action):
    """action()이 실패하면 장치 목록을 새로 읽고 한 번 더 한다. 두 번째 실패는 그대로 던진다.

    프로세스를 다시 시작하지 않아도 다시 꽂은 마이크·늦게 뜬 PipeWire를 쓰게 하기 위해서다.
    """
    try:
        return action()
    except Exception as e:
        log.warning("%s 실패(%s). 오디오 장치 목록을 새로 읽고 한 번 더 시도합니다.", what, e)
    _refresh_devices(sd)
    return action()


class AudioDevice:
    name = "base"

    def play_wav(self, path: Union[str, Path]) -> None:
        """WAV 파일을 끝까지 재생한다(블로킹). 실패하면 AudioError."""
        raise NotImplementedError

    def record(self, seconds: float, sample_rate: int) -> bytes:
        """seconds초 녹음해 16비트 모노 WAV 파일 바이트로 돌려준다. 실패하면 AudioError."""
        raise NotImplementedError


class SoundDeviceAudio(AudioDevice):
    """실제 스피커·마이크. 장치는 번호나 이름 일부로 지정한다('python -m sounddevice'로 목록 확인)."""

    name = "sounddevice"

    def __init__(self, input_device: Optional[Union[str, int]] = None,
                 output_device: Optional[Union[str, int]] = None):
        self.input_device = _parse_device(input_device)
        self.output_device = _parse_device(output_device)

    def play_wav(self, path: Union[str, Path]) -> None:
        path = Path(path)
        if not path.is_file():
            raise AudioError(f"안내 음성 파일 없음: {path}")
        try:
            self._play_sounddevice(path)
            return
        except AudioError as e:
            # 출력 장치 설정이 맞지 않아도 ALSA 기본 장치(aplay)로는 나올 수 있다.
            log.warning("sounddevice 재생 실패(%s), aplay로 다시 시도합니다", e)
        self._play_aplay(path)

    def _play_sounddevice(self, path: Path) -> None:
        sd = _import_sounddevice()
        try:
            with wave.open(str(path), "rb") as w:
                if w.getsampwidth() != 2:
                    raise AudioError("16비트 WAV가 아닙니다")
                channels = w.getnchannels()
                rate = w.getframerate()
                data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, channels)
        except (wave.Error, EOFError, ValueError) as e:
            raise AudioError(f"WAV 파일을 읽을 수 없음: {e}") from e
        try:
            _with_device_retry(sd, "재생", lambda: sd.play(data, samplerate=rate, device=self.output_device,
                                                           blocking=True))
        except Exception as e:  # PortAudioError 등: 장치 없음, 형식 미지원
            raise AudioError(f"재생 실패: {e}") from e

    def _play_aplay(self, path: Path) -> None:
        # 비정상적으로 오래 걸리면 녹음이 늦어지므로 넉넉한 시간 제한을 둔다.
        try:
            with wave.open(str(path), "rb") as w:
                duration = w.getnframes() / float(w.getframerate() or 1)
        except (wave.Error, EOFError):
            duration = 30.0
        try:
            subprocess.run(["aplay", "-q", str(path)], check=True, timeout=duration + 10.0,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            raise AudioError("aplay가 없습니다(sudo apt install alsa-utils)") from e
        except subprocess.TimeoutExpired as e:
            raise AudioError("aplay 재생 시간 초과") from e
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or b"").decode("utf-8", "replace").strip()
            raise AudioError(f"aplay 재생 실패: {detail or e.returncode}") from e

    def record(self, seconds: float, sample_rate: int) -> bytes:
        frames = int(round(float(seconds) * int(sample_rate)))
        if frames <= 0:
            raise AudioError(f"녹음 시간이 잘못됨: {seconds}초")
        sd = _import_sounddevice()

        def rec():
            samples = sd.rec(frames, samplerate=int(sample_rate), channels=1, dtype="int16",
                             device=self.input_device)
            sd.wait()
            return samples

        try:
            samples = _with_device_retry(sd, "녹음", rec)
        except Exception as e:  # PortAudioError: 마이크 없음, 장치 사용 중 등
            raise AudioError(f"마이크 녹음 실패: {e}") from e
        data = wav_bytes(np.asarray(samples, dtype=np.int16), sample_rate)
        log.info("녹음 완료 (%.1f초, 최대 음량 %.0f%%)", seconds, peak_level(data) * 100)
        return data


class FakeAudio(AudioDevice):
    """테스트 전용. 녹음할 때마다 replies에서 하나씩 꺼낸다.

    문장(str) → UTF-8 바이트(서버의 가짜 음성 인식기가 그대로 문장으로 읽는다)
    None      → AudioError (마이크 고장 흉내)
    목록이 비면 b"" (아무 말도 없음)
    play_error=True면 재생할 때 AudioError (안내 음성 실패 흉내).
    """

    name = "fake"

    def __init__(self, replies: Optional[List[Optional[str]]] = None, play_error: bool = False):
        self._replies: List[Optional[str]] = list(replies or [])
        self._lock = threading.Lock()  # to_thread로 동시에 불려도 같은 응답을 두 번 꺼내지 않도록
        self.play_error = play_error
        self.played: List[Path] = []
        self.recorded: List[float] = []  # record()에 넘어온 seconds

    def play_wav(self, path: Union[str, Path]) -> None:
        with self._lock:
            self.played.append(Path(path))
        if self.play_error:
            raise AudioError("가짜 스피커: 재생 실패")

    def record(self, seconds: float, sample_rate: int) -> bytes:
        with self._lock:
            self.recorded.append(seconds)
            if not self._replies:
                return b""
            reply = self._replies.pop(0)
        if reply is None:
            raise AudioError("가짜 마이크: 녹음 실패")
        return reply.encode("utf-8")


def create_audio(settings, fake_replies: Optional[List[Optional[str]]] = None) -> AudioDevice:
    """설정(audio_backend)에 맞는 장치를 만든다.

    sounddevice를 쓸 수 없어도 여기서는 멈추지 않는다(경고만). 카메라 전송은 계속해야 하고,
    음성 확인 때 녹음 실패를 서버에 보고하면 '확인 불가' 알림이 나가기 때문이다.
    """
    backend = (settings.audio_backend or "sounddevice").strip().lower()
    if backend == "fake":
        log.warning("가짜 오디오를 사용합니다(테스트 전용). 실제로 녹음하지 않습니다.")
        return FakeAudio(fake_replies)
    if backend == "sounddevice":
        try:
            _import_sounddevice()
        except AudioError as e:
            log.warning("%s. 'sudo apt install libportaudio2'를 확인해 주세요. "
                        "음성 확인 때는 '확인 불가'로 보고됩니다.", e)
        return SoundDeviceAudio(settings.input_device, settings.output_device)
    raise AudioError(f"알 수 없는 오디오 종류: {settings.audio_backend!r} (sounddevice, fake 중 하나)")


def _self_test(seconds: float, out: Path) -> int:
    """설치 확인용: 안내 음성을 틀고 녹음해 파일로 저장한 뒤 최대 음량을 보여 준다."""
    from pi_client.config import ClientSettings

    settings = ClientSettings()
    audio = SoundDeviceAudio(settings.input_device, settings.output_device)
    print(f"안내 음성 재생: {settings.prompt_wav}")
    try:
        audio.play_wav(settings.prompt_wav)
    except AudioError as e:
        print(f"  재생 실패: {e} (녹음은 계속합니다)")
    print(f"{seconds:.0f}초 녹음합니다. 평소 목소리로 말해 보세요...")
    try:
        data = audio.record(seconds, settings.sample_rate)
    except AudioError as e:
        print(f"녹음 실패: {e}")
        print("마이크 연결과 'arecord -l' 목록, PI_INPUT_DEVICE 값을 확인해 주세요.")
        return 1
    out.write_bytes(data)
    peak = peak_level(data)
    db = 20 * np.log10(peak) if peak > 0 else float("-inf")
    print(f"저장: {out}  (재생: aplay {out})")
    print(f"최대 음량: {peak * 100:.0f}% ({db:.1f} dBFS)")
    if peak < 0.05:
        print("소리가 너무 작습니다. alsamixer(F4: 녹음)에서 마이크 감도를 올려 주세요.")
    elif peak >= 0.99:
        print("소리가 잘립니다(클리핑). alsamixer에서 마이크 감도를 조금 내려 주세요.")
    else:
        print("적당합니다.")
    return 0


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Pi 스피커·마이크 확인")
    parser.add_argument("--test", action="store_true", help="안내 음성 재생 후 녹음해 최대 음량을 보여 준다")
    parser.add_argument("--seconds", type=float, default=3.0, help="녹음 시간(초), 기본 3")
    parser.add_argument("--out", type=Path, default=Path("/tmp/pi_record_test.wav"), help="녹음 저장 경로")
    args = parser.parse_args()
    if not args.test:
        parser.print_help()
        sys.exit(0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(_self_test(args.seconds, args.out))
