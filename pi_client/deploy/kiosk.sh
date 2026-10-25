#!/bin/bash
# 현장 화면(Chromium 키오스크)을 띄운다. kiosk.service가 실행한다.
# 필요한 환경변수(pi-client.env): PI_SERVER_URL, PI_DEVICE_ID, PI_DEVICE_KEY, (선택) PI_CA_FILE
set -euo pipefail

: "${PI_SERVER_URL:?PI_SERVER_URL이 없습니다 (pi-client.env 확인)}"
: "${PI_DEVICE_ID:?PI_DEVICE_ID가 없습니다 (pi-client.env 확인)}"
: "${PI_DEVICE_KEY:?PI_DEVICE_KEY가 없습니다 (pi-client.env 확인)}"
server="${PI_SERVER_URL%/}"

# 화면 세션이 준비될 때까지 기다린다. 사용자 서비스에는 DISPLAY/WAYLAND_DISPLAY가 늦게 들어올 수 있다.
for _ in $(seq 1 30); do
    if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; then
        break
    fi
    while IFS='=' read -r key value; do
        case "${key}" in
            DISPLAY|WAYLAND_DISPLAY|XAUTHORITY) export "${key}=${value}" ;;
        esac
    done < <(systemctl --user show-environment 2>/dev/null || true)
    if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; then
        break
    fi
    sleep 1
done
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    echo "화면 세션을 찾지 못했습니다. 데스크톱 자동 로그인이 켜져 있는지 확인하세요(raspi-config)." >&2
    exit 1
fi

# 서버가 응답할 때까지 기다린다. HTTP 응답이 오면(상태 코드와 관계없이) 서버는 살아 있는 것이다.
# 기기 키는 주소가 아니라 헤더로 보낸다(프로세스 목록에 덜 남도록).
curl_args=(-s -o /dev/null -w '%{http_code}' --max-time 5 -H "X-Device-Key: ${PI_DEVICE_KEY}")
if [ -n "${PI_CA_FILE:-}" ]; then
    curl_args+=(--cacert "${PI_CA_FILE}")
fi
status_url="${server}/api/devices/${PI_DEVICE_ID}/state"
waited=0
while true; do
    code="$(curl "${curl_args[@]}" "${status_url}" || true)"
    if [ -n "${code}" ] && [ "${code}" != "000" ]; then
        break
    fi
    if [ $((waited % 30)) -eq 0 ]; then
        echo "서버(${server}) 응답을 기다리는 중... (TLS 오류라면 PI_CA_FILE과 인증서 설치를 확인)" >&2
    fi
    sleep 2
    waited=$((waited + 2))
done
case "${code}" in
    200) ;;
    401|403) echo "서버가 기기 키를 거부했습니다(${code}). 화면에 오류가 보일 수 있습니다. 기기 ID·키를 확인하세요." >&2 ;;
    404) echo "서버에 기기 ${PI_DEVICE_ID}가 없습니다(404). 마이페이지에서 기기 ID를 확인하세요." >&2 ;;
    *) echo "서버 응답 코드 ${code}. 그래도 화면을 띄웁니다." >&2 ;;
esac

# Raspberry Pi OS 버전에 따라 이름이 다르다.
browser=""
for name in chromium-browser chromium; do
    if command -v "${name}" >/dev/null 2>&1; then
        browser="$(command -v "${name}")"
        break
    fi
done
if [ -z "${browser}" ]; then
    echo "Chromium이 설치되어 있지 않습니다: sudo apt install chromium-browser (또는 chromium)" >&2
    exit 1
fi

# 현장 화면은 로그인 대신 기기 키로 접속한다(웹 쪽 /kiosk 페이지가 key를 읽는다).
kiosk_url="${server}/kiosk?device_id=${PI_DEVICE_ID}&key=${PI_DEVICE_KEY}"

# --incognito: 비정상 종료 뒤 '페이지 복원' 창이 뜨지 않게 한다.
# --autoplay-policy: 화면을 누르지 않아도 안내음·경보음을 재생할 수 있게 한다.
# --password-store=basic: 시작할 때 키링 비밀번호 창이 뜨지 않게 한다.
# --ozone-platform-hint=auto: Wayland(Bookworm 기본)와 X11 어느 쪽에서도 뜨게 한다.
exec "${browser}" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --incognito \
    --password-store=basic \
    --autoplay-policy=no-user-gesture-required \
    --check-for-update-interval=31536000 \
    --ozone-platform-hint=auto \
    "${kiosk_url}"
