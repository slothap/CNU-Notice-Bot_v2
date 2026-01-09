import os
import time
import json
import requests
import re
import traceback
from datetime import datetime
from dotenv import load_dotenv
import random
import json as pyjson

# ===[셀레니움 관련 라이브러리]===
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

# ===[설정 영역]==========================
# 1시간 주기 (초)
CHECK_INTERVAL = 3600

# [재시도 설정]
MAX_RETRIES = 3
RETRY_DELAY = 60

USER_ID = os.environ.get("CNU_ID")
USER_PW = os.environ.get("CNU_PW")
DISCORD_WEBHOOK_URL = os.environ.get("with_WEBHOOK_URL")
MONITOR_WEBHOOK_URL = os.environ.get("MONITOR_WEBHOOK_URL")

LIST_URL = "https://with.cnu.ac.kr/ptfol/imng/icmpNsbjtPgm/findIcmpNsbjtPgmList.do"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
DATA_FILE = os.path.join(DATA_DIR, "with_data.json")

PROFILE_DIR = os.path.join(BASE_DIR, "chrome_profile")
if not os.path.exists(PROFILE_DIR):
    os.makedirs(PROFILE_DIR)
# ==========================================

# === [함수] ===
def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()

def parse_str_to_dt(date_str):
    if not date_str: return None
    try:
        if ":" in date_str:
            return datetime.strptime(date_str, "%Y.%m.%d %H:%M")
        else:
            return datetime.strptime(date_str, "%Y.%m.%d")
    except:
        return None

# [new] 멀티 프로그램 정보 계산 (인정시간 최대값 로직)
def calculate_multi_info(sub_items):
    if not sub_items: return None
    app_ends, oper_starts, oper_ends, capacities = [], [], [], []
    time_values = [] # 인정시간 숫자들을 담을 리스트

    for item in sub_items:
        # 1. 신청 기간
        if item['apply_raw']:
            parts = item['apply_raw'].split('~')
            if len(parts) > 1:
                dt = parse_str_to_dt(parts[1].strip())
                if dt: app_ends.append(dt)
        # 2. 운영 기간
        if item['oper_raw']:
            parts = item['oper_raw'].split('~')
            if len(parts) > 0:
                dt_s = parse_str_to_dt(parts[0].strip())
                if dt_s: oper_starts.append(dt_s)
            if len(parts) > 1:
                dt_e = parse_str_to_dt(parts[1].strip())
                if dt_e: oper_ends.append(dt_e)
            elif len(parts) == 1 and dt_s:
                oper_ends.append(dt_s)
        
        # 3. 정원
        if item['capacity']:
            nums = re.findall(r'\d+', item['capacity'])
            if nums: capacities.append(int(nums[0]))
        
        # 4. [NEW] 인정시간 숫자 추출
        if item['time_raw']:
            # "3.0 시간", "2시간" 등에서 숫자(소수점 포함) 추출
            t_nums = re.findall(r"[\d\.]+", item['time_raw'])
            if t_nums:
                try: time_values.append(float(t_nums[0]))
                except: pass

    result = {"apply": "", "oper": "", "capacity": "", "max_time": ""}
    
    if app_ends:
        result['apply'] = f"~{min(app_ends).strftime('%m.%d')}"
    if oper_starts and oper_ends:
        min_s, max_e = min(oper_starts), max(oper_ends)
        if min_s.date() == max_e.date():
            result['oper'] = f"{min_s.strftime('%m.%d %H:%M')}~{max_e.strftime('%H:%M')}"
        else:
            result['oper'] = f"{min_s.strftime('%m.%d')}~{max_e.strftime('%m.%d')}"
    if capacities:
        result['capacity'] = f"{min(capacities)}명"
    
    # [NEW] 인정시간 중 가장 큰 값 선택
    if time_values:
        max_t = max(time_values)
        # 소수점이 .0이면 정수로 변환gka (3.0 -> 3)
        if max_t.is_integer():
            result['max_time'] = f"{int(max_t)}시간"
        else:
            result['max_time'] = f"{max_t}시간"
            
    return result

# [new] 상세 정보 추출 - 인정시간 추가됨
def extract_details(container):
    data = {"apply_raw": "", "oper_raw": "", "capacity": "", "time_raw": ""}
    
    # 1. 상단 정보 (.etc_info_txt) - 신청/운영 기간
    try:
        # .etc_info_txt 내부의 dl
        for dl in container.find_elements(By.CSS_SELECTOR, ".etc_info_txt dl"):
            dt = dl.find_element(By.TAG_NAME, "dt").get_attribute("textContent")
            dd = dl.find_element(By.TAG_NAME, "dd").get_attribute("textContent")
            
            if "신청" in dt: 
                data["apply_raw"] = clean_text(dd)
            elif "운영" in dt or "교육기간" in dt: 
                data["oper_raw"] = clean_text(dd)
    except: pass

    # 2. 하단 정보 ().rq_desc) - 정원 및 인정시간
    try:
        #.rq_desc 내부의 dl
        rq_desc = container.find_element(By.CSS_SELECTOR, ".rq_desc")
        
        # (1) 정원 찾기
        for dl in rq_desc.find_elements(By.TAG_NAME, "dl"):
            dt_text = dl.find_element(By.TAG_NAME, "dt").get_attribute("textContent")
            if "모집" in dt_text or "정원" in dt_text:
                data["capacity"] = clean_text(dl.find_element(By.TAG_NAME, "dd").get_attribute("textContent"))

        # (2) [NEW] 인정시간 찾기 (dl class="mileage") 이렇게 생김!
        try:
            mileage_dl = rq_desc.find_element(By.CLASS_NAME, "mileage")
            # 텍스트 추출 (예: "3.0 시간")
            data["time_raw"] = clean_text(mileage_dl.find_element(By.TAG_NAME, "dd").get_attribute("textContent"))
        except: 
            pass # mileage 클래스가 없을 경우 패스 (오류 방지!)

    except: pass
    
    return data

# === [디스코드 알림 함수] ===
def post_to_discord_safe(content):
    if not DISCORD_WEBHOOK_URL: return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        print("✉ [알림 전송 성공]")
    except Exception as e:
        print(f"⚠ [알림 전송 실패] {e}")

def send_simple_error_log(error_msg=None, is_fatal=False):
    if not MONITOR_WEBHOOK_URL: return
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    title = "🚨 **[WITH 봇 치명적 오류]**" if is_fatal else "⚠ **[WITH 봇 경고]**"
    content = f"{title}\n시간: {now}\n"
    if error_msg: content += f"내용: ```{error_msg}```"
    if is_fatal: content += "\n> 📢 **모든 재시도 실패. 봇 점검이 필요합니다.**"
    try: requests.post(MONITOR_WEBHOOK_URL, json={"content": content}, timeout=5)
    except: pass

# [핵심] 메시지 생성 함수 (인정시간 표시 추가)
def create_message_content(info):
    icon = "▶" if info['is_multi'] else "▷"
    d_day_part = f"{info['d_day']} | " if info['d_day'] else ""
    header = f"** {icon} {d_day_part}[{info['title']}](<{info['link']}>) **\n"
    body_lines = []

    if info['is_multi'] and info['sub_items']:
        first_sub = info['sub_items'][0]['title']
        count = len(info['sub_items']) - 1
        sub_text = f"[{first_sub}] 외 {count}개 반" if count > 0 else f"[{first_sub}]"
        body_lines.append(sub_text)

    parts = []
    def simple_date(raw):
        m = re.search(r'\d{4}\.(\d{2}\.\d{2})', raw)
        return m.group(1) if m else raw

    def format_single_period(raw, is_apply=False):
        if not raw: return ""
        p = raw.split('~')
        if len(p) < 2: return raw
        s, e = simple_date(p[0]), simple_date(p[1])
        return f"~{e}" if is_apply else f"{s}~{e}"

    apply_txt, oper_txt, cap_txt, time_txt = "", "", "", ""
    
    if info['is_multi']:
        apply_txt = info['multi_calc']['apply']
        oper_txt = info['multi_calc']['oper']
        cap_txt = info['multi_calc']['capacity']
        time_txt = info['multi_calc']['max_time'] # 계산된 최대 시간
    else:
        apply_txt = format_single_period(info['apply_raw'], True)
        oper_txt = format_single_period(info['oper_raw'], False)
        cap_txt = info['capacity']
        # "3.0 시간" 등에서 숫자만 깔끔하게 남기고 싶다면 여기서도 정리 가능하지만, raw도 괜찮음
        time_txt = info['time_raw'] 

    if apply_txt: parts.append(f"신청: {apply_txt}")
    if oper_txt: parts.append(f"운영: {oper_txt}")
    if cap_txt: parts.append(f"정원: {cap_txt}")
    if time_txt: parts.append(f"인정: {time_txt}") # [NEW] 알림 메시지에 추가

    if parts: body_lines.append(" | ".join(parts))

    body_text = ""
    for line in body_lines:
        body_text += f"> {line}\n"
    return header + body_text + "\n"

def send_batch_messages(new_items):
    if not new_items: return
    count = len(new_items)
    full_message = f"### :compass: [CNU With+] 새로운 비교과 {count}건\n\n"
    for item in reversed(new_items):
        content_chunk = create_message_content(item)
        if len(full_message) + len(content_chunk) > 1900:
            post_to_discord_safe(full_message)
            full_message = ""
        full_message += content_chunk
    if full_message: post_to_discord_safe(full_message)

# === [브라우저 생성 함수] ===
def create_driver():
    chrome_options = Options()
    # 로컬 테스트 시 브라우저 창을 보고 싶다면 아래 headless 줄을 주석 처리(#) 하세요
    # chrome_options.add_argument("--headless") 
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # [핵심] 프로필 유지
    chrome_options.add_argument(f"user-data-dir={PROFILE_DIR}")

    # === [수정된 부분: 드라이버 경로 자동 선택] ===
    server_driver_path = "/usr/bin/chromedriver"
    
    if os.path.exists(server_driver_path):
        # 1. 서버 환경 (파일이 존재함)
        print(f"💻 서버 환경 감지: {server_driver_path} 사용")
        service = Service(server_driver_path)
    else:
        # 2. 로컬 환경 (파일이 없음 -> 자동 관리)
        # Selenium 4.6+ 버전부터는 드라이버를 지정하지 않으면 알아서 설치/실행합니다.
        print("💻 로컬 환경 감지: 드라이버 자동 관리 모드 사용")
        service = Service() 

    return webdriver.Chrome(service=service, options=chrome_options)
def login_process(driver, wait):
    driver.get("https://with.cnu.ac.kr/index.do")
    try:
        if len(driver.find_elements(By.CLASS_NAME, "login_btn")) == 0:
            print("☑ 자동 로그인 성공 (세션 유지)")
            return
    except: pass
    print("☐ 로그인 시도 중...")
    try:
        login_btn = wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "login_btn")))
        driver.execute_script("arguments[0].click();", login_btn)
    except: pass
    try:
        try:
            wait.until(EC.visibility_of_element_located((By.NAME, "userId"))).send_keys(USER_ID)
            driver.find_element(By.NAME, "password").send_keys(USER_PW + Keys.RETURN)
        except:
            found = False
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                try:
                    driver.find_element(By.NAME, "userId").send_keys(USER_ID)
                    driver.find_element(By.NAME, "password").send_keys(USER_PW + Keys.RETURN)
                    found = True
                    driver.switch_to.default_content()
                    break
                except: continue
            if not found: raise Exception("로그인 폼 찾기 실패")
        try:
            wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "login_btn")))
            print("☑ 신규 로그인 성공")
        except:
            raise Exception("로그인 버튼 미소멸")
    except Exception as e:
        raise e

# === [메인 로직] ===
def perform_scraping_cycle():
    driver = None
    try:
        driver = create_driver()
        wait = WebDriverWait(driver, 20)
        login_process(driver, wait)

        last_read_id = None
        is_first = False
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    last_read_id = json.load(f).get("last_read_id")
            except: pass
        if not last_read_id: is_first = True

        driver.get(LIST_URL)
        time.sleep(random.uniform(2, 4))

        new_items = []
        stop = False
        top_id = None

        for page in range(1, 4):
            if stop: break
            if page > 1:
                try:
                    driver.execute_script(f"global.page({page});")
                    time.sleep(random.uniform(2, 4))
                except: break

            items = driver.find_elements(By.CSS_SELECTOR, "li:has(div.cont_box)")
            if not items:
                items = [li for li in driver.find_elements(By.CSS_SELECTOR, "li") if li.find_elements(By.CLASS_NAME, "cont_box")]
            if not items: continue

            for item in items:
                try:
                    a_tag = item.find_element(By.CSS_SELECTOR, "a.tit")
                    pid = ""
                    try: pid = pyjson.loads(a_tag.get_attribute("data-params")).get("encSddpbSeq")
                    except: pass
                    if not pid: continue
                    if top_id is None: top_id = pid
                    if pid == last_read_id:
                        stop = True
                        break
                    if is_first: continue

                    link = f"https://with.cnu.ac.kr/ptfol/imng/icmpNsbjtPgm/findIcmpNsbjtPgmInfo.do?encSddpbSeq={pid}&paginationInfo.currentPageNo=1"
                    full_title = a_tag.get_attribute("textContent")
                    try: title = clean_text(full_title.replace(a_tag.find_element(By.CLASS_NAME, "label").get_attribute("textContent"), ""))
                    except: title = clean_text(full_title)
                    try: d_day = clean_text(item.find_element(By.CSS_SELECTOR, "span.day").get_attribute("textContent"))
                    except: d_day = ""

                    is_multi = "multi_class" in item.get_attribute("class")
                    p_data = {
                        "id": pid, "title": title, "d_day": d_day, "link": link,
                        "is_multi": is_multi, "sub_items": [], "multi_calc": {},
                        "apply_raw": "", "oper_raw": "", "capacity": "", "time_raw": ""
                    }

                    try:
                        more = item.find_elements(By.CLASS_NAME, "class_more_open")
                        if more and more[0].is_displayed():
                            driver.execute_script("arguments[0].click();", more[0])
                            time.sleep(0.5)
                    except: pass

                    if is_multi:
                        for sub in item.find_elements(By.CLASS_NAME, "class_cont"):
                            if not sub.get_attribute("textContent").strip(): continue
                            try:
                                s_title = sub.find_element(By.CSS_SELECTOR, "a.tit").get_attribute("textContent")
                                try: s_title = s_title.replace(sub.find_element(By.CLASS_NAME, "label").get_attribute("textContent"), "")
                                except: pass
                                p_data['sub_items'].append({"title": clean_text(s_title), **extract_details(sub)})
                            except: continue
                        p_data['multi_calc'] = calculate_multi_info(p_data['sub_items'])
                    else:
                        p_data.update(extract_details(item))

                    new_items.append(p_data)
                except: continue

        if is_first:
            if top_id:
                with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump({"last_read_id": top_id}, f)
            print("☐ 최초 실행 - 기준점 설정 완료")
        elif new_items:
            print(f"● {len(new_items)}개 새 글 발견")
            send_batch_messages(new_items)
            if top_id:
                with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump({"last_read_id": top_id}, f)
        else:
            print("☒ 새 글 없음")

    except Exception as e:
        raise e
    finally:
        if driver:
            try: driver.quit()
            except: pass

def run_selenium_scraper():
    print(f"🚀 WITH(비교과) 봇 시작 (주기: {CHECK_INTERVAL}초, 재시도: {MAX_RETRIES}회)")
    try:
        while True:
            print("\n" + "━" * 40)
            print(f"⏰ 검사 시작: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            success = False
            last_error = ""
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    perform_scraping_cycle()
                    success = True
                    break
                except Exception as e:
                    last_error = str(e)
                    print(f"⚠ [시도 {attempt}/{MAX_RETRIES}] 에러: {e}")
                    if attempt < MAX_RETRIES:
                        print(f"⏳ {RETRY_DELAY}초 후 재시도...")
                        time.sleep(RETRY_DELAY)
            if not success:
                error_msg = f"{MAX_RETRIES}회 재시도 실패.\n마지막 에러: {last_error}\n{traceback.format_exc()}"
                print("❌ 모든 재시도 실패. 관리자 알림 전송.")
                send_simple_error_log(error_msg, is_fatal=True)
            print(f"💤 {CHECK_INTERVAL}초 대기 중...")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("\n👋 봇을 종료합니다.")

if __name__ == "__main__":
    run_selenium_scraper()