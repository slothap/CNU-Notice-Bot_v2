import requests
from bs4 import BeautifulSoup
import os
import time
import json
import re
import urllib3
import traceback
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

# [설정 영역]
# 30분 주기
CHECK_INTERVAL = 1800

# [NEW] 재시도 설정
MAX_RETRIES = 3
RETRY_DELAY = 60

DISCORD_WEBHOOK_URL = os.environ.get("library_WEBHOOK_URL")
MONITOR_WEBHOOK_URL = os.environ.get("MONITOR_WEBHOOK_URL")

URL = "https://library.cnu.ac.kr/bbs/list/1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
DATA_FILE = os.path.join(DATA_DIR, "library_data.json")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
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
def extract_id_from_link(link):
    match_under = re.search(r'_(\d+)$', link)
    if match_under: return int(match_under.group(1))
    
    match_slash = re.search(r'/(\d+)$', link)
    if match_slash: return int(match_slash.group(1))
    return 0

# ===[디코 전송기]===
def send_discord_message(new_notices):
    if not new_notices or not DISCORD_WEBHOOK_URL: return

    count = len(new_notices)
    message_content = f"### :books: [일반공지] 새 글 {count}건\n\n"

    for notice in new_notices:
        title = notice['title']
        link = notice['link']
        icon = "▶" if notice['is_top'] else "▷"
        message_content += f"{icon} [{title}](<{link}>)\n"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message_content}, timeout=10)
        print(f"✉ [전송 완료] 도서관 공지 {count}건")
    except Exception as e:
        print(f"⚠ [전송 실패] {e}")

# ===[관리자 알림 함수]===
def send_simple_error_log(error_msg=None, is_fatal=False):
    """
    is_fatal=True 일 때만 관리자 호출 (@everyone 등 필요시 메시지에 추가 가능)
    """
    if not MONITOR_WEBHOOK_URL: return

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    title = "🚨 **[도서관 봇 치명적 오류]**" if is_fatal else "⚠ **[도서관 봇 경고]**"
    
    content = f"{title}\n시간: {now}\n"
    if error_msg: content += f"에러: ```{error_msg}```"
    if is_fatal: content += "\n> 📢 **모든 재시도 실패. 봇 점검이 필요합니다.**"

    try: requests.post(MONITOR_WEBHOOK_URL, json={"content": content}, timeout=5)
    except: pass

# ===[핵심 로직]===
def check_library_notices(session, saved_data):
    """
    성공 시: True/False 반환 (변경사항 유무)
    실패 시: Exception 발생 (상위 루프에서 재시도)
    """
    print(f"⌕ [도서관] 공지 확인 중...")

    # 여기서 에러나면 상위 try-except로 넘어감
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(URL, headers=HEADERS, verify=False, timeout=30)
    response.encoding = 'utf-8'

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.select('tbody > tr')
    
    if not rows:
        raise Exception("게시글(tr) 없음 - HTML 구조 변경 의심")

    last_id = saved_data.get("last_id", 0)
    new_notices = []
    max_id_in_this_scan = last_id

    for row in rows:
        a_tag = row.select_one('td.title a') or row.select_one('td.subject a') or row.select_one('a')
        if not a_tag: continue

        title = a_tag.get('title') or a_tag.text.strip()
        title = title.replace("새글", "").strip()

        href = a_tag.get('href')
        link = f"https://library.cnu.ac.kr{href}"

        article_id = extract_id_from_link(link)
        if article_id == 0: continue

        is_top = 'always' in row.get('class', [])

        if article_id > last_id:
            new_notices.append({
                "id": article_id, "title": title, "link": link, "is_top": is_top
            })
            if article_id > max_id_in_this_scan:
                max_id_in_this_scan = article_id

    # 최초 실행
    if last_id == 0 and max_id_in_this_scan > 0:
        print(f"☐ [도서관] 최초 실행 - 기준점(ID: {max_id_in_this_scan})만 설정")
        saved_data["last_id"] = max_id_in_this_scan
        return True

    # 새 글 전송
    if new_notices:
        new_notices.sort(key=lambda x: x['id'])
        send_discord_message(new_notices)
        saved_data["last_id"] = max_id_in_this_scan
        return True

    return False

# ===[MAIN]===
def run_bot():
    print(f"🚀 도서관 봇 시작 (주기: {CHECK_INTERVAL}초, 재시도: {MAX_RETRIES}회)")

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
            success = False

            # [재시도 로직]
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if check_library_notices(session, saved_data):
                        any_changes = True
                    success = True
                    break # 성공하면 반복문 탈출
                except Exception as e:
                    print(f"⚠ [시도 {attempt}/{MAX_RETRIES}] 실패: {e}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)
            
            # 재시도 모두 실패 시
            if not success:
                send_simple_error_log("3회 접속/파싱 실패", is_fatal=True)
            elif any_changes:
                # 성공했고 변경사항이 있을 때만 저장
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(saved_data, f, indent=4)
                print("☑ 데이터 저장 완료")
            else:
                print("☒ 새 소식 없음")

            print(f"💤 {CHECK_INTERVAL}초 대기 중...")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n👋 봇을 종료합니다.")
    except Exception as e:
        print(f"⚠ 치명적 오류: {e}")
        send_simple_error_log(f"메인 루프 종료됨\n{e}", is_fatal=True)

if __name__ == "__main__":
    run_bot()