#!/bin/bash
# Raspberry Pi(Raspberry Pi OS Bookworm)에 Pi 클라이언트를 설치한다. 여러 번 실행해도 된다.
#
#   cd ~/elderly_fall_detection_pi_client && bash pi_client/deploy/install.sh            # 클라이언트 + 현장 화면
#   bash pi_client/deploy/install.sh --no-kiosk                            # 화면 없는 Pi
#
# 하는 일: apt 패키지 → 가상환경(.venv) → pip 패키지 → 설정 파일(pi-client.env) →
#          인증 기관(PI_CA_FILE) 설치 → systemd 서비스 등록.
# root가 아니라 데스크톱에 로그인하는 사용자(pi)로 실행한다. 필요한 곳에서만 sudo를 쓴다.
set -euo pipefail

INSTALL_KIOSK=1
for arg in "$@"; do
    case "${arg}" in
        --no-kiosk) INSTALL_KIOSK=0 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "알 수 없는 옵션: ${arg}" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    echo "root로 실행하지 마세요. 서비스를 돌릴 사용자(pi)로 실행하면 필요한 곳에서 sudo를 묻습니다." >&2
    exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${DEPLOY_DIR}/../.." && pwd)"
RUN_USER="$(id -un)"
RUN_UID="$(id -u)"
ENV_FILE="${DEPLOY_DIR}/pi-client.env"
VENV="${REPO_DIR}/.venv"
# 서비스 파일에 적어 둔 기본 위치. 저장소가 다른 곳에 있으면 복사할 때 바꿔 넣는다.
DEFAULT_DIR="/home/pi/elderly_fall_detection_pi_client"
CA_NAME="fall-detect-ca"

step() { echo; echo "==> $*"; }
warn() { echo "[경고] $*" >&2; }

# 서비스 파일의 경로·사용자·uid를 이 Pi에 맞게 바꿔 출력한다.
render_unit() {
    sed -e "s|${DEFAULT_DIR}|${REPO_DIR}|g" \
        -e "s|^User=pi$|User=${RUN_USER}|" \
        -e "s|/run/user/1000|/run/user/${RUN_UID}|g" \
        "$1"
}

# pi-client.env에서 값 하나를 읽는다(마지막 줄 우선, 양끝 따옴표 제거).
env_value() {
    sed -n "s/^[[:space:]]*$1=//p" "${ENV_FILE}" | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

# ---------------------------------------------------------------------------
step "[1/6] apt 패키지 설치"
sudo apt-get update
# libportaudio2: sounddevice(녹음·재생), alsa-utils: aplay·arecord·alsamixer,
# libnss3-tools: Chromium에 인증 기관을 넣는 certutil, curl: 현장 화면이 서버를 기다릴 때 사용.
sudo apt-get install -y python3-venv python3-pip libportaudio2 alsa-utils curl libnss3-tools ca-certificates
# 카메라 모듈 라이브러리. pip로는 설치할 수 없어 apt로 넣는다. Pi가 아닌 기기에는 없을 수 있다.
if ! sudo apt-get install -y python3-picamera2; then
    warn "python3-picamera2를 설치하지 못했습니다. USB 웹캠(PI_CAMERA=opencv)은 그대로 쓸 수 있습니다."
fi
if [ "${INSTALL_KIOSK}" -eq 1 ]; then
    # Raspberry Pi OS 버전에 따라 패키지 이름이 다르다.
    if apt-cache show chromium-browser >/dev/null 2>&1; then
        sudo apt-get install -y chromium-browser
    else
        sudo apt-get install -y chromium
    fi
fi
# 카메라·소리 장치 접근 권한(기본 pi 사용자는 이미 들어 있다).
sudo usermod -aG video,audio "${RUN_USER}"

# ---------------------------------------------------------------------------
step "[2/6] 가상환경 ${VENV}"
# --system-site-packages: apt로 설치한 picamera2를 가상환경에서도 import할 수 있게 한다.
if [ ! -x "${VENV}/bin/python" ]; then
    python3 -m venv --system-site-packages "${VENV}"
elif ! grep -qi '^include-system-site-packages *= *true' "${VENV}/pyvenv.cfg"; then
    warn "${VENV}가 --system-site-packages 없이 만들어져 picamera2를 못 찾습니다."
    warn "카메라 모듈을 쓰려면 지우고 다시 실행하세요: rm -rf ${VENV} && bash $0"
fi

# ---------------------------------------------------------------------------
step "[3/6] pip 패키지 설치"
"${VENV}/bin/python" -m pip install --upgrade pip
"${VENV}/bin/python" -m pip install -r "${REPO_DIR}/pi_client/requirements.txt"

# ---------------------------------------------------------------------------
step "[4/6] 설정 파일 ${ENV_FILE}"
if [ ! -f "${ENV_FILE}" ]; then
    cp "${DEPLOY_DIR}/pi-client.env.example" "${ENV_FILE}"
    echo "예시를 복사했습니다. 서버 주소·기기 ID·기기 키를 채워 주세요: nano ${ENV_FILE}"
else
    echo "이미 있습니다(그대로 둡니다)."
fi
# 기기 키가 들어 있으므로 본인만 읽게 한다. systemd는 root로 이 파일을 읽으므로 문제없다.
chmod 600 "${ENV_FILE}"

# ---------------------------------------------------------------------------
step "[5/6] 인증 기관(PI_CA_FILE) 설치"
CA_FILE="$(env_value PI_CA_FILE)"
if [ -z "${CA_FILE}" ]; then
    echo "PI_CA_FILE이 비어 있어 건너뜁니다(공인 인증서 또는 http 시험)."
elif [ ! -f "${CA_FILE}" ]; then
    warn "PI_CA_FILE=${CA_FILE} 파일이 없습니다. 서버 PC의 'mkcert -CAROOT' 폴더에서 rootCA.pem을 복사한 뒤 다시 실행하세요."
else
    # 시스템 인증서 목록(curl, Python 기본값)에 넣는다. 확장자가 .crt여야 update-ca-certificates가 읽는다.
    sudo install -m 644 "${CA_FILE}" "/usr/local/share/ca-certificates/${CA_NAME}.crt"
    sudo update-ca-certificates
    if [ "${INSTALL_KIOSK}" -eq 1 ]; then
        # Chromium은 시스템 목록이 아니라 사용자 NSS DB(~/.pki/nssdb)를 본다.
        NSSDB="sql:${HOME}/.pki/nssdb"
        mkdir -p "${HOME}/.pki/nssdb"
        if [ ! -f "${HOME}/.pki/nssdb/cert9.db" ]; then
            certutil -N -d "${NSSDB}" --empty-password
        fi
        certutil -D -d "${NSSDB}" -n "${CA_NAME}" >/dev/null 2>&1 || true   # 다시 실행할 때 교체
        certutil -A -d "${NSSDB}" -n "${CA_NAME}" -t "C,," -i "${CA_FILE}"
        echo "Chromium에 인증 기관을 넣었습니다. Chromium이 켜져 있었다면 다시 시작해야 적용됩니다."
    fi
fi

# ---------------------------------------------------------------------------
step "[6/6] systemd 서비스 등록"
render_unit "${DEPLOY_DIR}/pi-client.service" | sudo tee /etc/systemd/system/pi-client.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable pi-client.service

if [ "${INSTALL_KIOSK}" -eq 1 ]; then
    chmod +x "${DEPLOY_DIR}/kiosk.sh"
    mkdir -p "${HOME}/.config/systemd/user" "${HOME}/.config/autostart"
    render_unit "${DEPLOY_DIR}/kiosk.service" > "${HOME}/.config/systemd/user/kiosk.service"
    # 데스크톱 세션에 따라 graphical-session.target이 켜지지 않기도 해서, 로그인할 때 직접 시작하는 항목도 둔다.
    # 화면 정보(DISPLAY/WAYLAND_DISPLAY)를 사용자 서비스에 넘긴 뒤 시작한다. 이미 떠 있으면 아무 일도 없다.
    cat > "${HOME}/.config/autostart/fall-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Fall detection kiosk
Exec=/bin/sh -c "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY; systemctl --user start kiosk.service"
X-GNOME-Autostart-enabled=true
DESKTOP
    if systemctl --user daemon-reload 2>/dev/null; then
        systemctl --user enable kiosk.service
    else
        warn "사용자 systemd에 연결하지 못했습니다. 데스크톱에 로그인한 뒤 실행하세요: systemctl --user daemon-reload && systemctl --user enable kiosk.service"
    fi
fi

# ---------------------------------------------------------------------------
echo
echo "설치 완료."
if [ "$(env_value PI_DEVICE_ID)" = "0" ] || [ -z "$(env_value PI_DEVICE_ID)" ] || [ -z "$(env_value PI_DEVICE_KEY)" ]; then
    echo "  1) 설정을 채우세요: nano ${ENV_FILE}  (PI_SERVER_URL, PI_DEVICE_ID, PI_DEVICE_KEY, PI_CA_FILE)"
    echo "     PI_CA_FILE을 새로 넣었다면 이 스크립트를 한 번 더 실행해 인증 기관을 설치하세요."
fi
echo "  - 클라이언트 시작/재시작: sudo systemctl restart pi-client"
echo "  - 클라이언트 로그:        journalctl -u pi-client -f"
if [ "${INSTALL_KIOSK}" -eq 1 ]; then
    echo "  - 현장 화면 시작:         systemctl --user start kiosk   (다음 로그인부터는 자동)"
fi
echo "  - 마이크·스피커 점검:     ${VENV}/bin/python -m pi_client.audio --test"
