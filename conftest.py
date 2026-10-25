"""테스트 공통 설정.

끝까지 테스트는 메인 저장소의 중앙 서버 코드(server/)를 띄운다. FALL_SERVER_REPO에 메인 저장소
경로를 주면 그 서버를 import할 수 있게 하고, 없으면 해당 테스트만 건너뛴다.
"""

import os
import sys
from pathlib import Path

_server_repo = os.getenv("FALL_SERVER_REPO")
if _server_repo:
    sys.path.insert(0, str(Path(_server_repo).expanduser().resolve()))
