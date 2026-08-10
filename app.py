import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# 日本時間（JST = UTC+9）のタイムゾーン定義
JST = timezone(timedelta(hours=9))

# 内閣府の祝日CSVファイル名
CSV_FILE_NAME = "syukujitsu.csv"

def check_is_holiday(date):
    month = date.month
    day = date.day
    day_of_week = date.weekday() # 0:月, 1:火, 2:水, 3:木, 4:金, 5:土, 6:日

    # ① 年末年始（12月30日〜1月3日）は無条件で土休日
    if (month == 12 and (day == 30 or day == 31)) or (month == 1 and (1 <= day <= 3)):
        return True

    # ② 土曜日・日曜日の判定
    if day_of_week == 5 or day_of_week == 6:
        return True

    # ③ 内閣府のCSVから日付（1列目）のみを読み込んで判定
    try:
        target_str = date.strftime('%Y/%m/%d') # 内閣府CSVは "2026/08/11" の形式
        
        if os.path.exists(CSV_FILE_NAME):
            # 文字化け対策として encoding='shift_jis' で読み込む
            with open(CSV_FILE_NAME, 'r', encoding='shift_jis', errors='ignore') as f:
                reader = csv.reader(f)
                next(reader) # ヘッダー行をスキップ
                for row in reader:
                    # row[0]（1列目の日付）のみをチェックし、2列目の祝日名は完全に無視する
                    if len(row) > 0 and row[0] == target_str:
                        return True
    except Exception as e:
        print(f"CSV Read Error: {e}")

    return False

def parse_timetable_from_file(file_path, direction):
    """GitHub内にあるテキストファイルから時刻表をパースする"""
    if not os.path.exists(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    timetable = []
    current_hour = 0
    
    # すべてのトークン（単語・数字）に分解して順番に処理する
    # 例: ["4時", "27", "40", "50", "5時", "01", ...] のようなフラットなリストにする
    tokens = content.split()
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        # 「〇時」という形式のトークンかチェック
        hour_match = re.search(r'(\d+)時', token)
        if hour_match:
            current_hour = int(hour_match.group(1))
            i += 1
            continue
            
        # 単に数字だけで、次のトークンが「時」のケース（まれにある構造対策）
        if token.isdigit() and i + 1 < len(tokens) and tokens[i+1] == "時":
            current_hour = int(token)
            i += 2
            continue

        # 種別（快）の処理
        train_type_prefix = ""
        if token == "快":
            train_type_prefix = "快"
            i += 1
            if i < len(tokens):
                token = tokens[i]
            else:
                break

        # 分数の数値かチェック
        if token.isdigit():
            minute = int(token)
            dest_code = ""
            
            # 次のトークンが宛先コード（浦、赤、上、蒲、磯、桜、神）かチェック
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
    elif code == "赤": return "赤羽"
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

    now = datetime.now(JST)
    current_hour = now.hour
    current_minute = now.minute
    current_second = now.second
    
    target_date = now
    if current_hour < 3:
        target_date = now - timedelta(days=1)

    current_total_sec = current_hour * 3600 + current_minute * 60 + current_second
    walk_time_sec = walk_minutes * 60

    is_holiday = check_is_holiday(target_date)
    
    # ダイヤに応じて読み込むファイルを切り替え
    if is_holiday:
        up_file = "timetable/d_i.txt"
        down_file = "timetable/d_o.txt"
        day_type_str = "土休日ダイヤ"
    else:
        up_file = "timetable/h_i.txt"
        down_file = "timetable/h_o.txt"
        day_type_str = "平日ダイヤ"

    try:
        up_timetable = parse_timetable_from_file(up_file, "up")
        next_up_trains = find_next_trains(up_timetable, current_total_sec, current_hour, walk_time_sec)

        down_timetable = parse_timetable_from_file(down_file, "down")
        next_down_trains = find_next_trains(down_timetable, current_total_sec, current_hour, walk_time_sec)
    except Exception as e:
        print(f"Error reading timetable files: {e}")
        next_up_trains = []
        next_down_trains = []

    return jsonify({
        "dayType": day_type_str,
        "up": next_up_trains,
        "down": next_down_trains
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
