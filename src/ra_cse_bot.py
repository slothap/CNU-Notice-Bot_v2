import requests
from bs4 import BeautifulSoup
import os
import time
import json
import re
import urllib3
import traceback
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

# ===[설정 영역]==========================
# 30분 주기로 바꿈
CHECK_INTERVAL = 1800

# [NEW] - 재시도하기
MAX_RETRIES = 3
RETRY_DELAY = 60

DISCORD_WEBHOOK_URL = os.environ.get("cse_WEBHOOK_URL")
MONITOR_WEBHOOK_URL = os.environ.get("MONITOR_WEBHOOK_URL")

# 데이터 파일 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
DATA_FILE = os.path.join(DATA_DIR, "cse_data.json")

# 게시판 목록
TARGET_BOARDS = [
    {"id": "bachelor", "name": "학사공지", "url": "https://computer.cnu.ac.kr/computer/notice/bachelor.do?articleLimit=30"},
    {"id": "general", "name": "교내일반소식", "url": "https://computer.cnu.ac.kr/computer/notice/notice.do?articleLimit=30"},
    {"id": "job", "name": "교외활동·인턴·취업", "url": "https://computer.cnu.ac.kr/computer/notice/job.do?articleLimit=30"},
    {"id": "project", "name": "사업단소식", "url": "https://computer.cnu.ac.kr/computer/notice/project.do?articleLimit=30"}
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Connection': 'keep-alive'
}
# ==========================================

# ===[세션 생성기]===
def get_session():
    """Network Level 재시도 세션"""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# ===[ID 추출기]===
def extract_article_id(link):
    match = re.search(r'articleNo=(\d+)', link)
    if match: return int(match.group(1))
    return 0

# ===[디코 전송기]===
def send_discord_batch_alert(category_name, new_notices):
    if not new_notices or not DISCORD_WEBHOOK_URL: return

    count = len(new_notices)
    message_content = f"### 📢 [{category_name}] 새 글 {count}건\n\n"

    for notice in new_notices:
        icon = "▶" if notice['is_top'] else "▷"
        message_content += f"{icon} [{notice['title']}](<{notice['link']}>)\n"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message_content}, timeout=5)
        print(f"✉ [전송 완료] {category_name} - {count}건")
    except Exception as e:
        print(f"⚠ [전송 실패] {e}")

# ===[관리자 알림 함수]===
def send_simple_error_log(error_msg=None, is_fatal=False):
    """
    is_fatal=True 일 때만 관리자 호출
    """
    if not MONITOR_WEBHOOK_URL: return

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    title = "🚨 **[CSE 봇 치명적 오류]**" if is_fatal else "⚠ **[CSE 봇 경고]**"
    
    content = f"{title}\n시간: {now}\n"
    if error_msg: content += f"에러: ```{error_msg}```"
    if is_fatal: content += "\n> 📢 **모든 재시도 실패. 봇 점검이 필요합니다.**"

    try: requests.post(MONITOR_WEBHOOK_URL, json={"content": content}, timeout=5)
    except: pass

# ===[게시판 검사]===
def check_board(session, board_info, saved_data):
    """
    성공 시: True/False 반환 (변경사항 유무)
    실패 시: Exception 발생 (상위 루프에서 재시도)
    """
    board_id = board_info["id"]
    board_name = board_info["name"]
    url = board_info["url"]

    print(f"● [{board_name}] 분석 중...")

    # [enw] 차단 방지 ~ 재시도 할때도 적용됨
    time.sleep(random.uniform(5, 10))

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(url, headers=HEADERS, verify=False, timeout=30)
    response.encoding = 'utf-8'

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.select('table.board-table tbody tr')

    if not rows:
        # 재시도 던지기
        raise Exception(f"게시글(tr) 없음 - HTML 구조 변경 또는 차단 의심")

    last_id = saved_data.get(board_id, 0)
    new_notices = []
    max_id = last_id

    for row in rows:
        title_div = row.select_one('.b-title-box > a')
        if not title_div: continue

        title = title_div.get('title') or title_div.text.strip()
        title = title.replace("자세히 보기", "").strip()

        href = title_div.get('href')
        if href.startswith('?'):
            base_url = url.split('?')[0]
            link = f"{base_url}{href}"
        else:
            link = href

        article_id = extract_article_id(link)
        if article_id == 0: continue

        row_classes = row.get('class', [])
        is_top = 'b-top-box' in row_classes

        if article_id > last_id:
            new_notices.append({
                "id": article_id, "title": title, "link": link, "is_top": is_top
            })
            if article_id > max_id: max_id = article_id

    # 최초 실행
    if last_id == 0 and max_id > 0:
        print(f"☐ [{board_name}] 최초 실행 - 기준점(ID: {max_id})만 설정")
        saved_data[board_id] = max_id
        return True

    # 새 글 전송
    if new_notices:
        new_notices.sort(key=lambda x: x['id'])
        send_discord_batch_alert(board_name, new_notices)
        saved_data[board_id] = max_id
        return True

    return False

# ===[MAIN]===
def run_bot():
    print(f"🚀 CSE 공지봇 시작 (주기: {CHECK_INTERVAL}초, 재시도: {MAX_RETRIES}회)")

    try:
        while True:
            print("\n" + "━" * 40)
            print(f"⏰ 검사 시작: {time.strftime('%Y-%m-%d %H:%M:%S')}")

            saved_data = {}
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, "r", encoding="utf-8") as f: saved_data = json.load(f)
                except: pass

            session = get_session()
            any_changes = False

            # 각 게시판 순회
            for board in TARGET_BOARDS:
                board_success = False
                
                # [new - 재시도 로직]
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        if check_board(session, board, saved_data):
                            any_changes = True
                        board_success = True
                        break # 성공 시 탈출
                    except Exception as e:
                        print(f"⚠ [{board['name']}] 실패 ({attempt}/{MAX_RETRIES}): {e}")
                        if attempt < MAX_RETRIES:
                            time.sleep(RETRY_DELAY)
                
                # 재시도 실패 시 관리자 알림
                if not board_success:
                    send_simple_error_log(f"[{board['name']}] 3회 접속 실패", is_fatal=True)

            if any_changes:
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(saved_data, f, ensure_ascii=False, indent=4)
                print("☑ 데이터 저장 완료")
            else:
                print("☒ 변동 사항 없음")

            print(f"💤 {CHECK_INTERVAL}초 대기 중...")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n👋 봇을 종료합니다.")
    except Exception as e:
        print(f"⚠ 치명적 오류: {e}")
        send_simple_error_log(f"메인 루프 종료됨\n{e}", is_fatal=True)

if __name__ == "__main__":
    run_bot()