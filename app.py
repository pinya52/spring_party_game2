import os
import base64
import io
import re
import requests as http_requests
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import numpy as np
from google import genai

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pictureguess-secret-2024')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

game_state = {
    'status': 'waiting',
    'questions': [],
    'current_question': 0,
    'participants': {},
    'answers': {},
    'canvas_data': None,
    'ai_image': None,
    'ai_style': None,
    'drawing_active': False,
}

MAX_PARTICIPANTS = 20
MAX_QUESTIONS = 50

def process_image(image_data_url, style):
    header, b64 = image_data_url.split(',', 1)
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    try:
        import cv2
        use_cv2 = True
    except ImportError:
        use_cv2 = False

    # ── 寫實：銳化 + 高對比 + 邊緣增強，模擬照片感
    if style == 'realistic':
        img = ImageEnhance.Sharpness(img).enhance(3.0)
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = ImageEnhance.Color(img).enhance(1.2)
        img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)

    # ── 吉卜力：柔化 + 高飽和度 + 暖色調，模擬手繪動畫感
    elif style == 'ghibli':
        img = img.filter(ImageFilter.SMOOTH_MORE)
        img = ImageEnhance.Color(img).enhance(1.6)
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Contrast(img).enhance(0.9)
        # 暖色調：紅綠微調
        r, g, b = img.split()
        r = ImageEnhance.Brightness(r).enhance(1.08)
        g = ImageEnhance.Brightness(g).enhance(1.03)
        img = Image.merge('RGB', (r, g, b))
        img = img.filter(ImageFilter.SMOOTH)

    # ── 漫畫：強邊緣 + 色塊化 + 高對比
    elif style == 'comic':
        if use_cv2:
            arr = np.array(img)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            blur = cv2.medianBlur(gray, 7)
            edges = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                          cv2.THRESH_BINARY, 9, 9)
            color = cv2.bilateralFilter(arr, 9, 300, 300)
            # 色塊化
            color = (color // 48) * 48
            edges_rgb = cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)
            result = cv2.bitwise_and(color, edges_rgb)
            img = Image.fromarray(result.astype(np.uint8))
        else:
            img = img.filter(ImageFilter.SMOOTH_MORE)
            img = ImageEnhance.Color(img).enhance(2.0)
            img = ImageEnhance.Contrast(img).enhance(1.8)
        img = ImageEnhance.Color(img).enhance(1.5)

    # ── 水彩：多次柔化 + 降飽和度 + 提亮，模擬水彩透明感
    elif style == 'watercolor':
        img = img.filter(ImageFilter.SMOOTH_MORE)
        img = img.filter(ImageFilter.SMOOTH_MORE)
        img = img.filter(ImageFilter.GaussianBlur(1))
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Color(img).enhance(0.75)
        img = ImageEnhance.Contrast(img).enhance(0.85)
        # 加淡淡紙張質感（輕微噪點模擬）
        arr = np.array(img, dtype=np.float32)
        noise = np.random.normal(0, 6, arr.shape)
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)

    # ── 油畫：模糊筆觸 + 高飽和度 + 浮雕感
    elif style == 'oil':
        img = img.filter(ImageFilter.GaussianBlur(2))
        img = ImageEnhance.Color(img).enhance(1.8)
        img = ImageEnhance.Contrast(img).enhance(1.3)
        img = img.filter(ImageFilter.EDGE_ENHANCE)
        # 模擬厚塗筆觸
        if use_cv2:
            arr = np.array(img)
            arr = cv2.bilateralFilter(arr, 15, 80, 80)
            img = Image.fromarray(arr)
        img = ImageEnhance.Sharpness(img).enhance(1.5)

    # ── 科幻：冷色調 + 高對比 + 邊緣光效
    elif style == 'scifi':
        img = ImageEnhance.Contrast(img).enhance(1.6)
        img = ImageEnhance.Sharpness(img).enhance(2.5)
        # 冷藍色調
        r, g, b = img.split()
        r = ImageEnhance.Brightness(r).enhance(0.75)
        g = ImageEnhance.Brightness(g).enhance(0.9)
        b = ImageEnhance.Brightness(b).enhance(1.35)
        img = Image.merge('RGB', (r, g, b))
        img = ImageEnhance.Color(img).enhance(1.4)
        img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)
        # 發光邊緣效果
        if use_cv2:
            arr = np.array(img)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            edges = cv2.Canny(gray, 80, 200)
            edges_colored = np.zeros_like(arr)
            edges_colored[:,:,2] = edges  # 藍色邊緣
            result = cv2.addWeighted(arr, 1.0, edges_colored, 0.6, 0)
            img = Image.fromarray(result)

    else:
        # 預設：輕度增強
        img = ImageEnhance.Sharpness(img).enhance(1.5)
        img = ImageEnhance.Contrast(img).enhance(1.1)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

STYLE_PROMPTS = {
    'realistic':  'high quality photo, colorful, detailed, based on sketch outline, realistic style',
    'ghibli':     'studio ghibli anime style, colorful, soft lighting, based on sketch outline, high quality',
    'watercolor': 'watercolor painting, soft colors, transparent wash, based on sketch outline, high quality',
    'comic':      'manga comic style, bold lines, colorful, based on sketch outline, high quality',
    'oil':        'oil painting, thick brushstrokes, vivid colors, based on sketch outline, high quality',
    'scifi':      'sci-fi concept art, neon colors, futuristic, based on sketch outline, high quality',
}
NEGATIVE_PROMPT = 'blurry, ugly, distorted, deformed, low quality, watermark, text'

def diffusion_generate(image_data_url, style, hf_token=None):
    """用 AI Horde (Stable Diffusion) 把草圖轉成精緻圖，不會有 429 錯誤"""
    import time
    import requests as http_requests

    # 1. 解析前端傳來的 Base64 草圖圖片
    header, b64 = image_data_url.split(',', 1)
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    
    # 將原始圖片存入記憶體準備發送
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    source_image_base64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

    STYLE_PROMPTS = {
        'realistic':  'high quality photo, colorful, detailed, realistic style',
        'ghibli':     'studio ghibli anime style, colorful, soft lighting, high quality',
        'watercolor': 'watercolor painting, soft colors, transparent wash, high quality',
        'comic':      'manga comic style, bold lines, colorful, high quality',
        'oil':        'oil painting, thick brushstrokes, vivid colors, high quality',
        'scifi':      'sci-fi concept art, neon colors, futuristic, high quality',
    }
    prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS['realistic'])
    NEGATIVE_PROMPT = 'blurry, ugly, distorted, deformed, low quality, watermark, text'

    # 2. 向 AI Horde 提交任務 (拿到號碼牌)
    submit_url = "https://aihorde.net/api/v2/generate/async"
    req_headers = {
        "apikey": "0000000000", # 使用預設匿名免費帳號
        "Content-Type": "application/json",
        "Client-Agent": "pictureguess:1.0",
    }
    
    payload = {
        "prompt": prompt + " ### " + NEGATIVE_PROMPT,
        "params": {
            "width": 512,
            "height": 512,
            "steps": 20,
            "sampler_name": "k_euler_a",
            "n": 1,
        },
        "nsfw": False,
        "censor_nsfw": True,
        "models": ["stable_diffusion"],
        "source_image": source_image_base64, # 將草圖作為墊圖
        "source_processing": "img2img",
    }
    
    resp = http_requests.post(submit_url, headers=req_headers, json=payload, timeout=30)
    if resp.status_code != 202:
        raise Exception(f"提交給 AI Horde 失敗，請稍後再試。")
    
    job_id = resp.json().get("id")

    # 3. 進入排隊輪詢機制 (每 3 秒問一次畫好了沒，最多等 60 秒)
    check_url  = f"https://aihorde.net/api/v2/generate/check/{job_id}"
    status_url = f"https://aihorde.net/api/v2/generate/status/{job_id}"
    
    for i in range(20):
        time.sleep(3)
        try:
            check = http_requests.get(check_url, headers=req_headers, timeout=10).json()
        except Exception:
            continue
            
        if check.get("faulted"):
            raise Exception("AI 繪圖任務失敗，請重試。")
        if check.get("done"):
            break
    else:
        raise Exception("排隊人數較多，生成逾時，請再試一次。")

    # 4. 取得完成的圖片
    result = http_requests.get(status_url, headers=req_headers, timeout=30).json()
    generations = result.get("generations", [])
    if not generations:
        raise Exception("未取得生成結果。")

    img_data = generations[0].get("img", "")
    
    if img_data.startswith("http"):
        img_resp = http_requests.get(img_data, timeout=30)
        raw_result = img_resp.content
    else:
        if "," in img_data:
            img_data = img_data.split(",", 1)[1]
        raw_result = base64.b64decode(img_data)

    result_img = Image.open(io.BytesIO(raw_result)).convert('RGB')
    out_buf = io.BytesIO()
    result_img.save(out_buf, format='PNG')
    
    return "data:image/png;base64," + base64.b64encode(out_buf.getvalue()).decode()

@app.route('/')
def index(): return render_template('admin.html')
@app.route('/admin')
def admin(): return render_template('admin.html')
@app.route('/show')
def show(): return render_template('show.html')
@app.route('/game')
def game(): return render_template('game.html')

@app.route('/api/state')
def api_state():
    return jsonify({
        'status': game_state['status'],
        'current_question': game_state['current_question'],
        'total_questions': len(game_state['questions']),
        'participant_count': len(game_state['participants']),
        'participants': [{'sid': sid, 'name': v['name'], 'score': v['score']} for sid, v in game_state['participants'].items()],
    })

@app.route('/api/upload_questions', methods=['POST'])
def upload_questions():
    data = request.json
    questions = data.get('questions', [])
    if len(questions) > MAX_QUESTIONS:
        return jsonify({'error': '題目不可超過 {} 題'.format(MAX_QUESTIONS)}), 400
    game_state['questions'] = questions
    return jsonify({'success': True, 'count': len(questions)})

@app.route('/api/process_image', methods=['POST'])
def api_process_image():
    data = request.json
    image_data = data.get('image')
    style = data.get('style', 'realistic')
    if not image_data:
        return jsonify({'error': '缺少圖片資料'}), 400
    try:
        result = process_image(image_data, style)
        game_state['ai_image'] = result
        game_state['ai_style'] = style
        q_idx = game_state['current_question']
        q = game_state['questions'][q_idx] if game_state['questions'] else {}
        socketio.emit('ai_image_generated', {
            'image': result,
            'style': style,
            'description': q.get('description', ''),
        })
        return jsonify({'success': True, 'image': result, 'style': style})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/diffusion', methods=['POST'])
def api_diffusion():
    """用 Stable Horde 把輪廓轉成細緻圖"""
    data = request.json
    image_data = data.get('image')
    style = data.get('style', 'realistic')
    if not image_data:
        return jsonify({'error': '缺少圖片資料'}), 400
    try:
        result = diffusion_generate(image_data, style)
        game_state['ai_image'] = result
        game_state['ai_style'] = style
        q_idx = game_state['current_question']
        q = game_state['questions'][q_idx] if game_state['questions'] else {}
        socketio.emit('ai_image_generated', {
            'image': result,
            'style': style,
            'description': q.get('description', ''),
        })
        return jsonify({'success': True, 'image': result, 'style': style})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/reset', methods=['POST'])
def api_reset():
    """重置遊戲狀態"""
    game_state['status'] = 'waiting'
    game_state['questions'] = []
    game_state['current_question'] = 0
    game_state['participants'] = {}
    game_state['answers'] = {}
    game_state['canvas_data'] = None
    game_state['ai_image'] = None
    game_state['ai_style'] = None
    game_state['drawing_active'] = False
    socketio.emit('game_reset', {})
    return jsonify({'success': True})

@socketio.on('connect')
def on_connect():
    emit('game_state', {
        'status': game_state['status'],
        'participants': [{'sid': sid, 'name': v['name'], 'score': v['score']} for sid, v in game_state['participants'].items()],
        'current_question': game_state['current_question'],
        'total_questions': len(game_state['questions']),
    })

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in game_state['participants']:
        name = game_state['participants'][sid]['name']
        del game_state['participants'][sid]
        plist = [{'sid': s, 'name': v['name'], 'score': v['score']} for s, v in game_state['participants'].items()]
        socketio.emit('participant_left', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('join_game')
def on_join_game(data):
    number = data.get('number', '').strip()
    sid = request.sid
    # 只接受 01–19 的兩位數字
    if not re.match(r'^(0[1-9]|1[0-9])$', number):
        emit('join_error', {'message': '請輸入 01–19 之間的數字（例如：05）'}); return
    name = number + '桌'
    existing = [v['name'] for v in game_state['participants'].values()]
    if name in existing:
        emit('join_error', {'message': f'{name} 已有人使用，請換一個號碼'}); return
    if len(game_state['participants']) >= MAX_PARTICIPANTS:
        emit('join_error', {'message': '已達人數上限 {} 組'.format(MAX_PARTICIPANTS)}); return
    game_state['participants'][sid] = {'name': name, 'score': 0, 'streak': 0, 'answered': False}
    plist = [{'sid': sid, 'name': v['name'], 'score': v['score']} for sid, v in game_state['participants'].items()]
    emit('join_success', {'name': name, 'status': game_state['status']})
    socketio.emit('participant_joined', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('request_rename')
def on_request_rename():
    """參賽者要求重新輸入名稱（只在遊戲開始前有效）"""
    sid = request.sid
    if game_state['status'] != 'waiting':
        emit('rename_error', {'message': '遊戲已開始，無法重新輸入'}); return
    if sid not in game_state['participants']:
        emit('rename_ok', {}); return
    name = game_state['participants'][sid]['name']
    del game_state['participants'][sid]
    plist = [{'sid': s, 'name': v['name'], 'score': v['score']} for s, v in game_state['participants'].items()]
    emit('rename_ok', {})
    socketio.emit('participant_left', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('admin_kick')
def on_admin_kick(data):
    """管理員移除參賽者"""
    target_name = data.get('name', '')
    target_sid = None
    for sid, v in game_state['participants'].items():
        if v['name'] == target_name:
            target_sid = sid
            break
    if not target_sid:
        return
    del game_state['participants'][target_sid]
    plist = [{'sid': s, 'name': v['name'], 'score': v['score']} for s, v in game_state['participants'].items()]
    socketio.emit('participant_left', {'name': target_name, 'participants': plist, 'count': len(plist)})
    # 通知被踢的人回到登入頁
    socketio.emit('kicked', {}, to=target_sid)

@socketio.on('admin_start_game')
def on_admin_start_game():
    if not game_state['questions']:
        emit('admin_error', {'message': '請先設定題目'}); return
    game_state['status'] = 'started'
    game_state['current_question'] = 0
    for sid in game_state['participants']:
        game_state['participants'][sid]['score'] = 0
        game_state['participants'][sid]['streak'] = 0
    _show_question()

@socketio.on('admin_open_answer')
def on_admin_open_answer():
    game_state['status'] = 'answering'
    game_state['answers'] = {}
    for sid in game_state['participants']:
        game_state['participants'][sid]['answered'] = False
    q = _current_question()
    socketio.emit('start_answering', {
        'image': game_state['ai_image'] or game_state['canvas_data'],
        'ai_style': game_state['ai_style'],
        'options': q['options'],
        'description': q['description'],
        'duration': 20,
    })

@socketio.on('admin_switch_image')
def on_admin_switch_image(data):
    """切換 show.html 答題畫面顯示 AI 圖或原始畫布"""
    mode = data.get('mode', 'ai')  # 'ai' or 'canvas'
    if mode == 'ai':
        image = game_state.get('ai_image') or game_state.get('canvas_data')
        ai_style = game_state.get('ai_style')
    else:
        image = game_state.get('canvas_data') or game_state.get('ai_image')
        ai_style = None
    socketio.emit('switch_image', {
        'image': image,
        'mode': mode,
        'ai_style': ai_style,
    })

@socketio.on('admin_next_question')
def on_admin_next_question():
    game_state['current_question'] += 1
    game_state['ai_image'] = None
    game_state['ai_style'] = None
    game_state['canvas_data'] = None
    game_state['drawing_active'] = False
    # ✅ 廣播清空畫布指令
    socketio.emit('clear_canvas')
    if game_state['current_question'] >= len(game_state['questions']):
        _finish_game()
    else:
        _show_question()

@socketio.on('drawing_update')
def on_drawing_update(data):
    game_state['canvas_data'] = data.get('image')
    game_state['drawing_active'] = True
    socketio.emit('canvas_update', {'image': data.get('image'), 'strokes': data.get('strokes')})

@socketio.on('submit_answer')
def on_submit_answer(data):
    sid = request.sid
    if sid not in game_state['participants']: return
    if game_state['status'] != 'answering': return
    if game_state['participants'][sid]['answered']: return

    answer = data.get('answer')
    time_taken = data.get('time_taken', 20)
    game_state['answers'][sid] = {'answer': answer, 'time_taken': time_taken}
    game_state['participants'][sid]['answered'] = True

    q = _current_question()
    correct = (answer == q['correct'])
    base = 100 if correct else 0
    time_bonus = max(0, int((20 - time_taken) * 5)) if correct else 0
    streak_bonus = 0
    if correct:
        game_state['participants'][sid]['streak'] += 1
        streak_bonus = game_state['participants'][sid]['streak'] * 10
    else:
        game_state['participants'][sid]['streak'] = 0
    total_gain = base + time_bonus + streak_bonus
    game_state['participants'][sid]['score'] += total_gain

    ranking = _get_ranking()
    my_rank = next((i+1 for i, r in enumerate(ranking) if r['sid'] == sid), 0)

    # 先儲存結果，等 show_result 時再顯示給參賽者
    emit('answer_received', {
        'correct': correct,
        'correct_answer': q['correct'],
        'base_score': base,
        'time_bonus': time_bonus,
        'streak_bonus': streak_bonus,
        'total_gain': total_gain,
        'total_score': game_state['participants'][sid]['score'],
        'rank': my_rank,
        'streak': game_state['participants'][sid]['streak'],
    })

    answered_count = sum(1 for v in game_state['participants'].values() if v['answered'])
    total_count = len(game_state['participants'])
    socketio.emit('answer_progress', {'answered': answered_count, 'total': total_count})

    # ✅ 全員答完自動公布
    if answered_count >= total_count and total_count > 0:
        _auto_show_result()

@socketio.on('admin_show_result')
def on_admin_show_result():
    _auto_show_result()

@socketio.on('timer_ended')
def on_timer_ended():
    # show.html 倒數結束時觸發，效果等同管理員手動公布
    _auto_show_result()

def _current_question():
    return game_state['questions'][game_state['current_question']]

def _auto_show_result():
    if game_state['status'] != 'answering':
        return
    game_state['status'] = 'result'
    q = _current_question()
    ranking = _get_ranking()
    correct_letter = q['correct']
    correct_text = q['options'][ord(correct_letter) - ord('A')]
    socketio.emit('show_result', {
        'correct_answer': correct_letter,
        'correct_text': correct_text,
        'ranking': ranking[:10],
    })

def _show_question():
    game_state['status'] = 'question'
    q = _current_question()
    socketio.emit('show_question', {
        'index': game_state['current_question'],
        'total': len(game_state['questions']),
        'description': q['description'],
        'options': q['options'],
    })

def _get_ranking():
    return sorted(
        [{'sid': sid, 'name': v['name'], 'score': v['score']} for sid, v in game_state['participants'].items()],
        key=lambda x: x['score'], reverse=True
    )

def _finish_game():
    game_state['status'] = 'finished'
    socketio.emit('game_finished', {'ranking': _get_ranking()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)