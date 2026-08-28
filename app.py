import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
import urllib.request
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# 日本時間（JST = UTC+9）のタイムゾーン定義
JST = timezone(timedelta(hours=9))

def check_is_holiday(date):
    month = date.month
    day = date.day
    day_of_week = date.weekday()

    if (month == 12 and (day == 30 or day == 31)) or (month == 1 and (1 <= day <= 3)):
        return True, "年末年始"

    if day_of_week == 5 or day_of_week == 6:
        return True, "土曜日" if day_of_week == 5 else "日曜日"

    try:
        url = "https://holidays-jp.github.io/api/v1/date.json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        
        with urllib.request.urlopen(req, timeout=3) as response:
            holidays = json.loads(response.read().decode('utf-8'))
            target_str = date.strftime('%Y-%m-%d')
            
            if target_str in holidays:
                return True, holidays[target_str] # 例: "山の日"
    except Exception as e:
        print(f"Holidays API Error: {e}")

    return False, ""

def parse_keikyu_timetable(content, direction):
    """京急形式（05:12\t普通\t浦賀行き 等）の時刻表テキストをパースする"""
    timetable = []
    lines = content.splitlines()
    current_hour = 0

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        # 「05時」などの時間見出し判定
        hour_header_match = re.match(r'^(\d{1,2})時$', line_str)
        if hour_header_match:
            current_hour = int(hour_header_match.group(1))
            continue

        # 「05:12\t普通\t浦賀行き」等の行判定
        time_match = re.match(r'^(\d{1,2}):(\d{2})\s+([^\s]+)\s+([^\s]+)', line_str)
        if time_match:
            h = int(time_match.group(1))
            m = int(time_match.group(2))
            train_type = time_match.group(3)
            dest = time_match.group(4)

            # 「行き」を削って統一表記にする
            dest = dest.replace("行き", "")

            adjusted_hour = h
            if 0 <= h < 3:
                adjusted_hour += 24 # 深夜帯のソート調整

            timetable.append({
                "hour": h,
                "sortHour": adjusted_hour,
                "minute": m,
                "type": train_type,
                "dest": dest
            })

    return timetable

def parse_jr_timetable(content, direction):
    """JR形式（既存の略称表記ベース）の時刻表テキストをパースする"""
    timetable = []
    current_hour = 0
    tokens = content.split()
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        hour_match = re.search(r'(\d+)時', token)
        if hour_match:
            current_hour = int(hour_match.group(1))
            i += 1
            continue
            
        if token.isdigit() and i + 1 < len(tokens) and tokens[i+1] == "時":
            current_hour = int(token)
            i += 2
            continue

        train_type_prefix = ""
        if token == "快":
            train_type_prefix = "快"
            i += 1
            if i < len(tokens):
                token = tokens[i]
            else:
                break

        if token.isdigit():
            minute = int(token)
            dest_code = ""
            
            if i + 1 < len(tokens) and tokens[i+1] in ["浦", "赤", "上", "蒲", "磯", "桜", "神"]:
                dest_code = tokens[i+1]
                i += 1

            full_line_text = train_type_prefix + token + dest_code
            parsed = parse_train_line(full_line_text, direction)
            
            if parsed is not None:
                adjusted_hour = current_hour
                if 0 <= current_hour < 3:
                    adjusted_hour += 24 # 深夜帯のソート調整
                
                timetable.append({
                    "hour": current_hour,
                    "sortHour": adjusted_hour,
                    "minute": parsed["minute"],
                    "type": parsed["type"],
                    "dest": parsed["dest"]
                })
        i += 1

    return timetable

def parse_timetable_from_file(file_path, direction, line_type="jr"):
    """ファイルから時刻表を読み込み、路線フォーマットに応じてパースする"""
    if not os.path.exists(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if line_type == "keikyu":
        timetable = parse_keikyu_timetable(content, direction)
    else:
        timetable = parse_jr_timetable(content, direction)

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
    elif code == "赤": return "赤羽"
    elif code == "上": return "上野"
    elif code == "蒲": return "蒲田"
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
    # URLパラメータ 'v' を取得
    v_param = request.args.get('v', '').lower()
    
    # 初期路線の決定 (デフォルトは 'jr')
    initial_line = 'keikyu' if v_param == 'kk' else 'jr'
    
    # OGP画像と説明文の決定
    base_desc = "浅野学園から新子安駅までの徒歩時間を設定して、&#10;自分がどの電車に乗れるか確かめよう！"

    if v_param == 'jr':
        og_images = ['embed.png']
        og_desc = f"{base_desc}&#10;&#10;※画像はイメージです。"
    elif v_param == 'kk':
        og_images = ['embed2.png']
        og_desc = f"{base_desc}&#10;&#10;※画像はイメージです。"
    else:
        og_images = []
        og_desc = base_desc  # 画像なし（その他）の場合は注記を含めない
        
    return render_template(
        'index.html', 
        initial_line=initial_line, 
        og_images=og_images,
        og_desc=og_desc
    )

@app.route('/api/trains')
def api_trains():
    walk_min_str = request.args.get('walk', '12')
    try:
        walk_minutes = int(walk_min_str)
    except ValueError:
        walk_minutes = 12

    # 路線パラメータを取得（jr または keikyu）
    line_type = request.args.get('line', 'jr')
    if line_type not in ['jr', 'keikyu']:
        line_type = 'jr'

    now = datetime.now(JST)
    current_hour = now.hour
    current_minute = now.minute
    current_second = now.second
    
    target_date = now
    if current_hour < 3:
        target_date = now - timedelta(days=1)

    current_total_sec = current_hour * 3600 + current_minute * 60 + current_second
    walk_time_sec = walk_minutes * 60

    is_holiday, holiday_name = check_is_holiday(target_date)
    
    timetable_dir = f"timetable-{line_type}"

    if is_holiday:
        up_file = os.path.join(timetable_dir, "d_i.txt")
        down_file = os.path.join(timetable_dir, "d_o.txt")
        day_type_str = "土休日ダイヤ"
    else:
        up_file = os.path.join(timetable_dir, "h_i.txt")
        down_file = os.path.join(timetable_dir, "h_o.txt")
        day_type_str = "平日ダイヤ"

    try:
        up_timetable = parse_timetable_from_file(up_file, "up", line_type)
        next_up_trains = find_next_trains(up_timetable, current_total_sec, current_hour, walk_time_sec)

        down_timetable = parse_timetable_from_file(down_file, "down", line_type)
        next_down_trains = find_next_trains(down_timetable, current_total_sec, current_hour, walk_time_sec)
    except Exception as e:
        print(f"Error reading timetable files: {e}")
        next_up_trains = []
        next_down_trains = []
        
    return jsonify({
        "line": line_type,
        "dayType": day_type_str,
        "isHoliday": is_holiday,
        "holidayName": holiday_name,
        "up": next_up_trains,
        "down": next_down_trains
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
