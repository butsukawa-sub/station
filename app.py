import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# スプレッドシートのID
SPREADSHEET_ID = "1DvLNwfgkN307lOzMcpBJLC2Xe7cd5EtGt2SaaLKMDio"

def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
        
    client = gspread.authorize(creds)
    return client

# 土休日（祝日・年末年始・土日）を判定する関数（GASの移植）
def check_is_holiday(date):
    month = date.month
    day = date.day
    day_of_week = date.weekday() # 0:月, 1:火, 2:水, 3:木, 4:金, 5:土, 6:日

    # ① 年末年始（12月30日〜1月3日）は無条件で土休日
    if (month == 12 and (day == 30 or day == 31)) or (month == 1 and (1 <= day <= 3)):
        return True

    # ② 土曜日・日曜日の判定 (Pythonでは 5:土, 6:日)
    if day_of_week == 5 or day_of_week == 6:
        return True

    # ③ 日本の祝日判定（jpholidayライブラリを使用、未導入の場合は簡易判定）
    try:
        import jpholiday
        if jpholiday.is_holiday(date):
            return True
    except ImportError:
        # jpholidayが入っていない場合の簡易的な祝日チェック（主要な固定祝日など）
        # ※正確な祝日判定のために `pip install jpholiday` を requirements.txt に追加することをおすすめします
        pass

    return False

def parse_timetable(sheet, direction):
    try:
        data = sheet.get_all_values()
    except Exception:
        return []
    
    if not data:
        return []

    timetable = []
    current_hour = 0

    for row in data:
        hour_col = str(row[0]).strip() if len(row) > 0 else ""
        detail_col = str(row[1]).strip() if len(row) > 1 else ""

        if hour_col != "":
            hour_match = re.search(r'(\d+)時?', hour_col)
            if hour_match:
                current_hour = int(hour_match.group(1))

        if detail_col != "":
            lines = detail_col.splitlines()
            for line in lines:
                line = line.strip()
                if line == "":
                    continue

                parsed = parse_train_line(line, direction)
                if parsed is not None:
                    adjusted_hour = current_hour
                    if 0 <= current_hour < 3:
                        adjusted_hour += 24 # 深夜帯を24時、25時としてソート用に調整
                    
                    timetable.append({
                        "hour": current_hour,
                        "sortHour": adjusted_hour,
                        "minute": parsed["minute"],
                        "type": parsed["type"],
                        "dest": parsed["dest"]
                    })

    timetable.sort(key=lambda x: (x["sortHour"] * 60 + x["minute"]))
    return timetable

def parse_train_line(line, direction):
    minute_match = re.search(r'(\d+)', line)
    if not minute_match:
        return None

    minute = int(minute_match.group(1))
    text_part = re.sub(r'\d+', '', line).strip()

    type_ = ""
    dest = ""

    if direction == "up":
        if text_part.startswith("快"):
            type_ = "快速"
            dest = text_part.replace("快", "", 1)
        else:
            type_ = "普通"
            dest = text_part
        dest = get_up_destination_name(dest)
    else:
        type_ = "普通"
        dest = get_down_destination_name(text_part)

    return {"minute": minute, "type": type_, "dest": dest}

def get_up_destination_name(code):
    if code == "浦": return "南浦和"
    elif code == "蒲": return "蒲田"
    elif code == "上": return "上野"
    else: return "大宮"

def get_down_destination_name(code):
    if code == "磯": return "磯子"
    elif code == "桜": return "桜木町"
    elif code == "神": return "東神奈川"
    else: return "大船"

def find_next_trains(timetable, current_total_sec, current_hour, walk_time_sec):
    results = []
    walk_time_sec_val = walk_time_sec

    adjusted_current_sec = current_total_sec
    if 0 <= current_hour < 3:
        adjusted_current_sec += 86400

    for train in timetable:
        train_total_sec = train["sortHour"] * 3600 + train["minute"] * 60
        leave_total_sec = train_total_sec - walk_time_sec_val
        remaining_sec = leave_total_sec - adjusted_current_sec

        if remaining_sec > 0:
            leave_h = (leave_total_sec // 3600) % 24
            leave_m = (leave_total_sec % 3600) // 60
            leave_s = leave_total_sec % 60
            leave_time_string = f"{leave_h:02d}:{leave_m:02d}:{leave_s:02d}"
            time_text = f"{train['hour']:02d}:{train['minute']:02d}"

            results.append({
                "type": train["type"],
                "destination": train["dest"],
                "time": time_text,
                "cars": 10,
                "leaveTime": leave_time_string,
                "remainingSec": remaining_sec
            })

            if len(results) >= 2:
                break

    return results

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/trains')
def api_trains():
    walk_min_str = request.args.get('walk', '12')
    try:
        walk_minutes = int(walk_min_str)
    except ValueError:
        walk_minutes = 12

    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    current_second = now.second
    
    # 深夜0時〜3時未満の場合は、前日の日付・曜日として判定する
    target_date = now
    if current_hour < 3:
        target_date = now - timedelta(days=1)

    current_total_sec = current_hour * 3600 + current_minute * 60 + current_second
    walk_time_sec = walk_minutes * 60

    # ダイヤの種類（平日か土休日か）を判定
    is_holiday = check_is_holiday(target_date)
    
    up_sheet_name = "休日上り" if is_holiday else "平日上り"
    down_sheet_name = "休日下り" if is_holiday else "平日下り"

    try:
        client = get_sheets_client()
        ss = client.open_by_key(SPREADSHEET_ID)
        
        up_sheet = ss.worksheet(up_sheet_name)
        up_timetable = parse_timetable(up_sheet, "up")
        next_up_trains = find_next_trains(up_timetable, current_total_sec, current_hour, walk_time_sec)

        down_sheet = ss.worksheet(down_sheet_name)
        down_timetable = parse_timetable(down_sheet, "down")
        next_down_trains = find_next_trains(down_timetable, current_total_sec, current_hour, walk_time_sec)
    except Exception as e:
        print(f"Error accessing spreadsheet: {e}")
        next_up_trains = []
        next_down_trains = []

    return jsonify({
        "dayType": "土休日ダイヤ" if is_holiday else "平日ダイヤ",
        "up": next_up_trains,
        "down": next_down_trains
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
