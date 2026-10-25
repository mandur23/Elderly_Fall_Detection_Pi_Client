# Pi 현장 단말 클라이언트

Raspberry Pi에서 돌리는 클라이언트입니다. 영상 분석과 음성 인식은 [중앙 서버](https://github.com/mandur23/Elderly_Fall_Detection_System/blob/claude/nifty-brahmagupta-od33zw/server/README.md)가 하고, Pi는 보고 듣고 말하는 일만 합니다. 전체 구조는 [docs/ARCHITECTURE.md](https://github.com/mandur23/Elderly_Fall_Detection_System/blob/claude/nifty-brahmagupta-od33zw/docs/ARCHITECTURE.md)에 있습니다.

> 상태: 테스트는 가짜 카메라·가짜 오디오로 실제 서버 코드에 붙여 통과합니다. 실제 Pi(카메라 모듈, USB 마이크, 스피커, Chromium 키오스크)에서는 아직 시험하지 않았습니다.

## 하는 일

1. 서버의 `wss://<서버>/ws/camera?device_id=N`에 접속합니다. 기기 키는 주소가 아니라 `X-Device-Key` 헤더로 보냅니다(서버 로그에 남지 않도록).
2. 카메라 프레임을 초당 약 10장(640×480 JPEG, 품질 70) 보냅니다. 네트워크가 느리면 밀린 프레임을 쌓지 않고 가장 최근 장면만 보냅니다.
3. 서버가 `voice_check`를 보내면 스피커로 안내 음성(`assets/ask_status.wav`)을 재생하고, 몇 초(기본 5초) 녹음해 `POST /api/events/{id}/voice`로 올립니다. 그동안에도 프레임은 계속 보냅니다.
   마이크가 없거나 녹음에 실패하면 녹음 대신 오류만 보내 서버가 바로 "확인 불가" 경보를 내게 합니다(응답 대기 시간을 기다리지 않음).
4. 서버가 꺼지거나 네트워크가 끊기면 1·2·5·10초 간격으로 계속 다시 접속합니다.
5. 카메라가 안 열리거나 멈추면 접속은 유지한 채 프레임만 멈추고(서버와 보호자 화면에 "카메라 응답 없음"이 뜸) 몇 초마다 카메라를 다시 엽니다.
6. Pi 화면에는 Chromium 키오스크로 서버의 현장 화면(`/kiosk`)을 띄웁니다. 상태 표시와 '괜찮아요' 버튼은 그 웹페이지가 담당합니다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `config.py` | 설정값(`PI_*` 환경변수) |
| `camera.py` | 카메라(picamera2·OpenCV 웹캠·영상 파일·가짜)와 JPEG 인코딩 |
| `audio.py` | 안내 음성 재생·녹음(sounddevice), 마이크 점검(`python -m pi_client.audio --test`) |
| `voice_check.py` | 음성 확인 한 번(재생 → 녹음 → 업로드) |
| `client.py` | 서버 접속·재접속, 프레임 전송, 서버 메시지 처리 |
| `__main__.py` | `python -m pi_client` 실행 진입점 |
| `deploy/pi-client.service` | 클라이언트 systemd 서비스 |
| `deploy/kiosk.service`, `deploy/kiosk.sh` | 현장 화면(Chromium 키오스크) 사용자 서비스와 실행 스크립트 |
| `deploy/pi-client.env.example` | 설정 파일 예시. `pi-client.env`로 복사해서 씀 |
| `deploy/install.sh` | 설치 스크립트(패키지, 가상환경, 인증서, 서비스) |
| `assets/` | 안내 음성 wav (저장소에는 올리지 않음) |

## 준비물

- Raspberry Pi 5(또는 4), Raspberry Pi OS **Bookworm** 64비트 데스크톱, 데스크톱 자동 로그인(`sudo raspi-config` → System Options → Boot / Auto Login → Desktop Autologin)
- 카메라 모듈(CSI) 또는 USB 웹캠, USB 마이크, 스피커, 디스플레이
- 같은 네트워크에서 돌고 있는 중앙 서버와, 서버 PC의 mkcert 인증 기관 파일 `rootCA.pem`

## 설치 순서

1. **서버 확인.** 서버 PC에서 서버를 HTTPS로 실행합니다([server/README.md](https://github.com/mandur23/Elderly_Fall_Detection_System/blob/claude/nifty-brahmagupta-od33zw/server/README.md) "실제 시연"). Pi에서 `curl -k -o /dev/null -w '%{http_code}\n' https://<서버 IP>:8443/api/me`가 `401`처럼 HTTP 상태 코드를 내면 서버까지는 닿는 것입니다(`000`이면 주소·방화벽 문제).
2. **기기 등록.** 휴대폰이나 PC 브라우저로 웹에 로그인 → 마이페이지 → 내 기기 → **Pi 기기 추가**. 화면에 나오는 **기기 ID**(숫자)와 **기기 키**를 적어 둡니다. 기기 키는 이때 한 번만 보입니다. 잃어버리면 그 기기를 지우고 새로 추가합니다.
3. **저장소 받기.** Pi에서 `pi` 사용자로:
   ```bash
   git clone https://github.com/mandur23/Elderly_Fall_Detection_Pi_Client ~/elderly_fall_detection_pi_client
   cd ~/elderly_fall_detection_pi_client
   ```
   다른 위치에 받아도 됩니다. `install.sh`가 서비스 파일의 경로·사용자를 실제 값으로 바꿔 등록합니다.
4. **인증 기관 파일 복사.** 서버 PC에서 `mkcert -CAROOT`가 알려 주는 폴더의 `rootCA.pem`을 Pi로 복사합니다.
   ```bash
   scp <서버PC>:<CAROOT>/rootCA.pem ~/elderly_fall_detection_pi_client/pi_client/deploy/rootCA.pem
   ```
   `rootCA-key.pem`(개인 키)은 **절대 복사하지 않습니다.**
5. **설치.**
   ```bash
   bash pi_client/deploy/install.sh          # 화면 없는 Pi는 --no-kiosk
   ```
   처음 실행하면 `pi_client/deploy/pi-client.env`가 예시에서 복사됩니다.
6. **설정 채우기.** `nano pi_client/deploy/pi-client.env`
   ```ini
   PI_SERVER_URL=https://192.168.0.10:8443
   PI_DEVICE_ID=3
   PI_DEVICE_KEY=<마이페이지에서 받은 키>
   PI_CA_FILE=/home/pi/elderly_fall_detection_pi_client/pi_client/deploy/rootCA.pem
   ```
   항목 설명은 [`deploy/pi-client.env.example`](deploy/pi-client.env.example)에 있습니다. 숫자 항목은 빈 값(`PI_FPS=`)으로 두지 말고, 기본값을 쓰려면 줄을 주석(`#`)으로 둡니다.
7. **설치 한 번 더.** `PI_CA_FILE`을 넣었으니 인증 기관을 시스템과 Chromium에 설치하도록 다시 실행합니다(여러 번 실행해도 됩니다).
   ```bash
   bash pi_client/deploy/install.sh
   ```
8. **안내 음성 넣기.** `pi_client/assets/ask_status.wav` (아래 "안내 음성 만들기").
9. **마이크·스피커 점검.** 아래 "마이크·스피커 점검".
10. **시작.**
    ```bash
    sudo systemctl restart pi-client
    journalctl -u pi-client -f              # 서버 접속 성공 로그가 보이는지 확인
    systemctl --user start kiosk            # 현장 화면. 다음 부팅부터는 자동
    ```
    웹 마이페이지(또는 보호자 화면)에서 그 기기가 "연결됨"으로 바뀌는지 확인합니다.

## 인증서(HTTPS)

서버는 mkcert로 만든 인증서를 씁니다. Pi가 그 인증서를 믿으려면 서버 PC의 인증 기관(`rootCA.pem`)이 필요합니다.

- 클라이언트(Python)는 `PI_CA_FILE`로 서버 인증서를 확인합니다. 비워 두면 시스템 인증서 목록으로 확인합니다.
- `install.sh`는 `PI_CA_FILE`을 `/usr/local/share/ca-certificates/fall-detect-ca.crt`로 복사해 시스템 목록(`update-ca-certificates`)에 넣고, Chromium이 보는 `~/.pki/nssdb`에도 `certutil`로 넣습니다.
- 서버 IP가 바뀌면 서버 PC에서 인증서를 그 IP로 다시 만들어야 합니다(`mkcert ... <새 IP>`). 인증 기관은 그대로라 Pi는 다시 설치할 필요가 없습니다.
- 확인: `curl --cacert pi_client/deploy/rootCA.pem -o /dev/null -w '%{http_code}\n' https://<서버 IP>:8443/api/me`가 `401`(로그인 안 함)처럼 상태 코드를 내면 인증서 확인이 된 것입니다. `000`과 함께 certificate 오류가 나오면 인증서 문제입니다.

## 마이크·스피커 점검

1. 장치 확인
   ```bash
   arecord -l                               # 마이크(녹음 장치) 목록
   aplay -l                                 # 스피커(재생 장치) 목록
   .venv/bin/python -m sounddevice          # sounddevice가 보는 장치 이름·번호
   ```
   기본 장치가 아닌 것을 쓰려면 `pi-client.env`에 `PI_INPUT_DEVICE`, `PI_OUTPUT_DEVICE`를 넣습니다. 장치 이름 일부(예: `USB PnP`)나 번호를 씁니다.
2. 녹음 시험
   ```bash
   set -a; . pi_client/deploy/pi-client.env; set +a
   .venv/bin/python -m pi_client.audio --test
   ```
   안내 음성을 재생한 뒤 3초 녹음해 `/tmp/pi_record_test.wav`에 저장하고 최고 음량(peak)을 출력합니다. `aplay /tmp/pi_record_test.wav`로 들어 봅니다.
3. 음량 맞추기: `alsamixer` → `F6`으로 USB 마이크 선택 → `F4`(Capture)에서 입력 음량 조절. 1~2 m 떨어져 "괜찮아요"라고 말했을 때 peak가 최대치의 30~70% 정도면 적당합니다. 너무 작으면 서버가 무응답으로 판단하고, 너무 크면 소리가 깨져 인식이 틀립니다. 스피커 음량은 `F3`(Playback)에서 맞춥니다.
4. 설정 저장: `sudo alsactl store` (재부팅 뒤에도 유지).

## 안내 음성 만들기

`pi_client/assets/ask_status.wav`. 형식은 WAV 16비트, **16 kHz 모노 권장**, 3~5초. 예: "괜찮으세요? 괜찮으시면 '괜찮아요'라고 말씀해 주세요."
wav 파일은 저장소에 올리지 않습니다(팀 공유 드라이브에 둡니다).

- Pi에서 직접 녹음
  ```bash
  arecord -f S16_LE -r 16000 -c 1 -d 5 pi_client/assets/ask_status.wav
  aplay pi_client/assets/ask_status.wav
  ```
- 노트북에서 TTS로 만들기(예: 무료 TTS 사이트나 OS 음성 합성으로 mp3/wav를 만든 뒤 변환)
  ```bash
  ffmpeg -i tts_output.mp3 -ar 16000 -ac 1 -sample_fmt s16 ask_status.wav
  # 또는 sox tts_output.wav -r 16000 -c 1 -b 16 ask_status.wav
  ```
  만든 파일을 `scp`로 Pi의 `pi_client/assets/`에 복사합니다.

파일이 없거나 재생에 실패해도 음성 확인은 진행됩니다(경고 로그를 남기고 바로 녹음, 현장 화면에 안내 문구 표시).

## 직접 실행 (서비스 없이)

서비스를 멈추고 터미널에서 실행하면 로그를 바로 볼 수 있습니다. 같은 기기로 두 개를 띄우면 서로 끊으므로 서비스는 꼭 멈춥니다.

실행 파일 `run_client.sh`(= `python run_client.py`)가 `pi_client/deploy/pi-client.env`를 읽어 실행합니다.

```bash
sudo systemctl stop pi-client
cd ~/elderly_fall_detection_pi_client
./run_client.sh --check      # 설정과 서버 연결만 확인 (카메라·마이크는 쓰지 않음)
./run_client.sh              # 실행, Ctrl+C로 종료
./run_client.sh --fake       # 카메라·마이크 없이 가짜 장치로 실행
```

`--check`는 서버 주소·기기 ID·기기 키가 맞는지 확인합니다. 결과: `0` 연결 확인 완료, `1` 서버에 닿지 않음(주소·Wi-Fi·인증서), `2` 설정 빠짐, `3` 서버가 거절(기기 ID·키가 틀렸거나 계정 정지). `--server`, `--device-id`, `--key`, `--env <파일>`로 값을 바꿔 줄 수도 있습니다.

## 장비 없이 시험 (PC 한 대)

카메라·마이크 없이 가짜 프레임과 가짜 응답으로 서버와의 흐름을 확인합니다. 서버는 [메인 저장소](https://github.com/mandur23/Elderly_Fall_Detection_System)에 있으므로 함께 받아 둡니다.

```bash
git clone https://github.com/mandur23/Elderly_Fall_Detection_System ~/Elderly_Fall_Detection_System
pip install -r ~/Elderly_Fall_Detection_System/server/requirements.txt -r pi_client/requirements.txt
```

아래 1번은 메인 저장소 폴더에서, 3번은 이 저장소 폴더에서 실행합니다.

1. 가짜 모델로 서버 실행(HTTP)
   ```bash
   FALL_POSE_BACKEND=fake FALL_STT_BACKEND=fake FALL_COOKIE_SECURE=0 python -m server
   ```
2. 사용자와 Pi 기기 만들기(웹 마이페이지가 있으면 거기서 해도 됩니다)
   ```bash
   curl -s -c /tmp/c.txt -H 'Content-Type: application/json' \
        -d '{"email":"test@example.com","password":"test-password","name":"시험"}' http://localhost:8443/api/signup
   curl -s -b /tmp/c.txt -H 'Content-Type: application/json' \
        -d '{"name":"거실 Pi","kind":"pi"}' http://localhost:8443/api/devices
   # 응답의 device.id와 key를 아래에 넣는다
   ```
3. 가짜 카메라·가짜 오디오로 클라이언트 실행
   ```bash
   PI_SERVER_URL=http://localhost:8443 PI_DEVICE_ID=<id> PI_DEVICE_KEY=<key> \
   PI_CAMERA=fake PI_AUDIO=fake python -m pi_client
   ```
   `PI_CAMERA=fake`는 서버의 가짜 자세 추정이 읽는 '서 있는 사람' 프레임을 반복해 보내므로 낙상은 일어나지 않습니다(접속·재접속·상태 확인용).
   서버 로그에 기기 접속이 보이고, `http://localhost:8443/api/devices/<id>/state`(헤더 `X-Device-Key: <key>`)에서 연결 상태를 볼 수 있습니다.
   실제 영상으로 시험하려면 `PI_CAMERA=file PI_VIDEO_PATH=<영상 파일>`을 쓰되, 서버도 실제 자세 추정(`FALL_POSE_BACKEND=yolo`)으로 실행해야 합니다(가짜 자세 추정은 JPEG가 아니라 관절 좌표 JSON을 받습니다).

낙상부터 음성 확인·해제·경보까지의 전체 흐름은 자동 테스트가 실제 서버 코드에 붙여 확인합니다. `FALL_SERVER_REPO`에 메인 저장소 경로를 주면 서버까지 띄우는 끝까지 테스트도 돌고, 없으면 그 테스트만 건너뜁니다.

```bash
pip install pytest
FALL_SERVER_REPO=~/Elderly_Fall_Detection_System python -m pytest pi_client/tests -q
```

## 서비스와 종료 코드

| 명령 | 내용 |
| --- | --- |
| `sudo systemctl restart pi-client` | 클라이언트 (재)시작 — 설정을 바꾼 뒤에도 |
| `journalctl -u pi-client -f` | 클라이언트 로그 |
| `systemctl --user restart kiosk` | 현장 화면 (재)시작 |
| `journalctl --user -u kiosk -f` | 현장 화면 로그 |

클라이언트는 다시 시작해도 소용없는 경우 아래 코드로 끝나고, systemd는 이 코드들은 재시작하지 않습니다(`RestartPreventExitStatus=2 3 4`). 원인을 고친 뒤 `sudo systemctl restart pi-client`로 직접 다시 시작합니다. 그 밖의 비정상 종료는 5초 뒤 자동으로 다시 시작합니다.

| 종료 코드 | 원인 | 확인할 것 |
| --- | --- | --- |
| 2 | 설정 누락(`PI_DEVICE_ID`, `PI_DEVICE_KEY`) | `pi-client.env` |
| 3 | 서버가 접속 거부: 4401 기기 키 오류, 4403 권한 없음 | 기기 ID·키 오타, 웹에서 기기를 지웠는지 |
| 4 | 같은 기기로 더 새로운 연결이 들어와 이 연결이 교체됨(4409) | 클라이언트가 두 개 떠 있는지(서비스 + 터미널, 다른 Pi에 같은 키) |

서버가 꺼졌거나 재시작한 경우, 네트워크 오류, 4400(잘못된 요청)은 종료하지 않고 계속 다시 접속합니다.

## 문제 해결

| 증상 | 원인과 해결 |
| --- | --- |
| 카메라를 열지 못한다는 로그 | 카메라 모듈: 케이블 방향, `rpicam-hello -t 2000`으로 확인. `python3-picamera2`가 설치됐는지, 가상환경이 `--system-site-packages`로 만들어졌는지(`install.sh`가 경고함). USB 웹캠: `ls /dev/video*`, `PI_CAMERA=opencv`, `PI_CAMERA_INDEX` |
| 카메라 모듈에서 `numpy.dtype size changed` | pip로 numpy 2가 들어감. `.venv/bin/pip install -r pi_client/requirements.txt`로 numpy 1.x로 되돌림 |
| 종료 코드 3, 로그에 4401 | 기기 키가 틀림. 키는 다시 볼 수 없으니 마이페이지에서 기기를 지우고 새로 추가한 뒤 ID·키를 모두 바꿈 |
| 종료 코드 4, 로그에 4409 | 같은 기기로 클라이언트가 두 개. `ps aux \| grep pi_client`, 터미널에서 띄운 것을 끄거나 서비스를 멈춤 |
| `CERTIFICATE_VERIFY_FAILED`, `certificate verify failed` | `PI_CA_FILE` 경로 확인, 서버 인증서에 지금 서버 IP가 들어 있는지(`mkcert ... <IP>`), Pi 시계가 맞는지(`timedatectl`) |
| `PI_SERVER_URL`이 http라는 경고 | 시험용 설정. 시연 때는 https로 |
| 현장 화면이 안 뜸 | `journalctl --user -u kiosk -f`. "서버 응답을 기다리는 중"이 계속되면 서버 주소·인증서 문제. 로그인 때 자동으로 안 뜨면 `~/.config/autostart/fall-kiosk.desktop`이 있는지, 데스크톱 자동 로그인이 켜져 있는지 |
| 현장 화면에 인증서 경고 | Chromium에 인증 기관이 없음. `PI_CA_FILE`을 넣고 `install.sh` 다시 실행 → `systemctl --user restart kiosk` |
| 안내 음성이 안 들림 / 녹음이 비어 있음 | "마이크·스피커 점검". 시스템 서비스에서만 안 되면 `PI_INPUT_DEVICE`·`PI_OUTPUT_DEVICE`에 USB 장치 이름을 직접 지정 |
| 음성 확인이 매번 "확인 불가" 경보 | 녹음 실패(로그 확인) 또는 서버의 음성 인식 모델 문제(서버 로그). 녹음 시간이 서버 대기 시간(`FALL_VOICE_TIMEOUT`)보다 길지 않은지 |
| 화면이 몇 분 뒤 꺼짐 | `sudo raspi-config` → Display Options → Screen Blanking → 끄기 |

## 보안 주의

- `pi-client.env`에는 기기 키가 있습니다. 저장소에 올리지 말고 권한 600을 유지합니다(`install.sh`가 맞춰 줌). 기기 키가 새면 마이페이지에서 기기를 지우면 키가 바로 무효가 됩니다.
- 현장 화면 주소(`/kiosk?device_id=..&key=..`)에도 기기 키가 들어가므로 Pi 안의 프로세스 목록에서 보일 수 있습니다. Pi 계정 비밀번호를 기본값으로 두지 않습니다. 서버는 접속 로그에서 키를 가립니다.
- `rootCA-key.pem`(인증 기관 개인 키)은 서버 PC 밖으로 옮기지 않습니다.
