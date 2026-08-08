import os
import json
from datetime import datetime, time, timedelta
from flask import Flask, render_template, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# スプレッドシートのID（ご自身のスプレッドシートURLの /d/ と /edit の間の文字列に書き換えてください）
SPREADSHEET_ID = "YOUR_SPREADSHEET_ID_HERE"

def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
    
    # Renderの環境変数（後述）からサービスアカウントのJSONを読み込む
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        # ローカルテスト用（同じ階層に service_account.json を置く場合）
        creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
        
    client = gspread.authorize(creds)
    return client

def check_is_holiday(date):
    month = date.month
    day = date.day
    day_of_week = date.weekday() # 0:月〜5:土, 6:日

    # 年末年始（12月30日〜1月3日）
    if (month == 12 and day >= 30) or (month == 1 and day <= 3):
        return True

    # 土曜日・日曜日
    if day_of_week >= 5:
        return True

    return False

def parse_train_line(line, direction):
    import re
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
        
        # 行先変換
        if dest == "浦": dest = "南浦和"
        elif dest == "蒲": dest = "蒲田"
        elif dest == "上": dest = "上野"
        else: dest = "大宮"
    else:
        type_ = "普通"
        if dest == "磯": dest = "磯子"
        elif dest == "桜": dest = "桜木町"
        elif dest == "神": dest = "東神奈川"
        else: dest = "大船"

    return {"minute": minute, "type": type_, "dest": dest}

def parse_timetable(sheet, direction):
    try:
        data = sheet.get_all_values()
    except Exception:
        return []
    
    timetable = []
    current_hour = 0

    for row in data:
        hour_col = str(row[0]).strip() if len(row) > 0 else ""
        detail_col = str(row[1]).strip() if len(row) > 1 else ""

        if hour_col:
            import re
            hour_match = re.search(r'(\d+)', hour_col)
            if hour_match:
                current_hour = int(hour_match.group(1))

        if detail_col:
            lines = detail_col.splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parsed = parse_train_line(line, direction)
                if parsed:
                    sort_hour = current_hour
                    if 0 <= current_hour < 3:
                        sort_hour += 24
                    timetable.append({
                        "hour": current_hour,
                        "sortHour": sort_hour,
                        "minute": parsed["minute"],
                        "type": parsed["type"],
                        "dest": parsed["dest"]
                    })

    timetable.sort(key=lambda x: (x["sortHour"] * 60 + x["minute"]))
    return timetable

def find_next_trains(timetable, current_total_sec, current_hour, walk_time_sec):
    results = []
    adjusted_current_sec = current_total_sec
    if 0 <= current_hour < 3:
        adjusted_current_sec += 86400

    for train in timetable:
        train_total_sec = train["sortHour"] * 3600 + train["minute"] * 60
        leave_total_sec = train_total_sec - walk_time_sec
        remaining_sec = leave_total_sec - adjusted_current_sec

        if remaining_sec > 0:
            leave_h = (leave_total_sec // 3600) % 24
            leave_m = (leave_total_sec % 3600) // 60
            leave_s = leave_total_sec % 60
            leave_time_str = f"{leave_h:02d}:{leave_m:02d}:{leave_s:02d}"
            time_text = f"{train['hour']:02d}:{train['minute']:02d}"

            results.append({
                "type": train["type"],
                "destination": train["dest"],
                "time": time_text,
                "cars": 10,
                "leaveTime": leave_time_str,
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

    target_date = now
    if current_hour < 3:
        target_date = now - timedelta(days=1)

    current_total_sec = current_hour * 3600 + current_minute * 60 + current_second
    walk_time_sec = walk_minutes * 60

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
