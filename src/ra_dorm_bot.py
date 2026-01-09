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
CHECK_INTERVAL = 1800

# [NEW] 재시도 설정
MAX_RETRIES = 3
RETRY_DELAY = 60

DISCORD_WEBHOOK_URL = os.environ.get("dorm_WEBHOOK_URL")
MONITOR_WEBHOOK_URL = os.environ.get("MONITOR_WEBHOOK_URL")

# 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
DATA_FILE = os.path.join(DATA_DIR, "dorm_data.json")

TARGET_BOARDS = [
    {"id": "movein", "name": "입주/퇴거 공지", "url": "https://dorm.cnu.ac.kr/_prog/_board/?code=sub05_0501&site_dvs_cd=kr&menu_dvs_cd=030101"},
    {"id": "general", "name": "일반공지", "url": "https://dorm.cnu.ac.kr/_prog/_board/?code=sub03_0301&site_dvs_cd=kr&menu_dvs_cd=0302"},
    {"id": "work", "name": "작업공지", "url": "https://dorm.cnu.ac.kr/_prog/_board/?code=sub03_0302&site_dvs_cd=kr&menu_dvs_cd=0303"}
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Connection': 'keep-alive',
    'Referer': 'https://dorm.cnu.ac.kr/'
}
# ==========================================

def get_session():
    """Network Level 재시도 세션"""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def extract_id_from_link(link):
    match = re.search(r'no=(\d+)', link)
    if match: return int(match.group(1))
    return 0

def send_discord_batch_alert(category_name, new_notices):
    if not new_notices or not DISCORD_WEBHOOK_URL: return
    count = len(new_notices)
    message_content = f"### 🛌 [{category_name}] 새 글 {count}건\n\n"
    for notice in new_notices:
        icon = "▶" if notice['is_top'] else "▷"
        message_content += f"{icon} [{notice['title']}](<{notice['link']}>)\n"
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message_content}, timeout=10)
        print(f"✉ [전송 완료] {category_name} - {count}건")
    except Exception as e:
        print(f"⚠ [전송 실패] {e}")

def send_simple_error_log(error_msg=None, is_fatal=False):
    """관리자 알림 함수 (치명적일 때만 강조)"""
    if not MONITOR_WEBHOOK_URL: return
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    title = "🚨 **[기숙사 봇 치명적 오류]**" if is_fatal else "⚠ **[기숙사 봇 경고]**"
    
    content = f"{title}\n시간: {now}\n"
    if error_msg: content += f"에러: ```{error_msg}```"
    if is_fatal: content += "\n📢 **모든 재시도 실패. 봇 점검이 필요합니다.**"

    try: requests.post(MONITOR_WEBHOOK_URL, json={"content": content}, timeout=5)
    except: pass

def check_board(session, board_info, saved_data):
    """
    성공 시: True/False (새 글 유무) 반환
    실패 시: Exception 발생 (상위 로직에서 재시도 처리)
    """
    board_id = board_info["id"]
    board_name = board_info["name"]
    url = board_info["url"]

    print(f"⌕ [{board_name}] 분석 중...")

    # SSL 경고 무시 (이건 나중에 알아보기)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # 여기서 에러나면 상위 try-except로 넘어감
    response = session.get(url, headers=HEADERS, verify=False, timeout=30)
    response.encoding = 'utf-8'

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.select('tbody > tr')
    
    # HTML 구조 변경 감지
    if not rows:
        raise Exception(f"게시글(tr) 없음 - HTML 구조 변경 의심")

    last_id = saved_data.get(board_id, 0)
    new_notices = []
    max_id = last_id

    for row in rows:
        title_td = row.select_one('td.title')
        if not title_td: continue
        a_tag = title_td.select_one('a')
        if not a_tag: continue

        title = a_tag.get('title') or a_tag.text.strip()
        href = a_tag.get('href')

        if href.startswith("?"): link = f"https://dorm.cnu.ac.kr/_prog/_board/{href}"
        elif href.startswith("/"): link = f"https://dorm.cnu.ac.kr{href}"
        else: link = f"https://dorm.cnu.ac.kr/_prog/_board/{href}"

        article_id = extract_id_from_link(link)
        if article_id == 0: continue

        is_top = "공지" in row.select_one('td.num').get_text() if row.select_one('td.num') else False

        if article_id > last_id:
            new_notices.append({
                "id": article_id, "title": title, "link": link, "is_top": is_top
            })
            if article_id > max_id: max_id = article_id

    # 최초 실행 처리
    if last_id == 0 and max_id > 0:
        print(f"☐ [{board_name}] 최초 실행 - 기준점 설정 (ID: {max_id})")
        saved_data[board_id] = max_id
        return True # 저장 필요 (꼭 필요 - 파일 생성 안될 때 있음)

    if new_notices:
        new_notices.sort(key=lambda x: x['id'])
        send_discord_batch_alert(board_name, new_notices)
        saved_data[board_id] = max_id
        return True

    return False

def run_bot():
    print(f"🚀 기숙사 봇 시작 (주기: {CHECK_INTERVAL}초, 재시도: {MAX_RETRIES}회)")

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
                time.sleep(random.uniform(1, 2)) # 게시판 사이 대기
                
                # [재시도 로직 적용]
                board_success = False
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        if check_board(session, board, saved_data):
                            any_changes = True
                        board_success = True
                        break # 성공 시 루프 탈출
                    except Exception as e:
                        print(f"⚠ [{board['name']}] 실패 ({attempt}/{MAX_RETRIES}): {e}")
                        if attempt < MAX_RETRIES:
                            time.sleep(RETRY_DELAY)
                
                # 재시도 전부 실패 시 관리자 알림
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