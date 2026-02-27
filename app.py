import os
import base64
import io
import re
import time
import requests as http_requests

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import numpy as np

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pictureguess-secret-2024')
CORS(app, resources={r"/*": {"origins": "*"}})

socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='gevent',  
    ping_timeout=60,  
    ping_interval=25,  
    max_http_buffer_size=10e6,  
    logger=False,  
    engineio_logger=False
)

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
    'last_canvas_update': 0,  
}

MAX_PARTICIPANTS = 20
MAX_QUESTIONS = 50
CANVAS_UPDATE_THROTTLE = 0.1  

def optimize_image_for_transfer(img, max_size=600, quality=75):
    if img.width > max_size or img.height > max_size:
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def process_image(image_data_url, style):
    header, b64 = image_data_url.split(',', 1)
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    try:
        import cv2
        use_cv2 = True
    except ImportError:
        use_cv2 = False

    if style == 'realistic':
        img = ImageEnhance.Sharpness(img).enhance(3.0)
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = ImageEnhance.Color(img).enhance(1.2)
        img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)
    elif style == 'ghibli':
        img = img.filter(ImageFilter.SMOOTH_MORE)
        img = ImageEnhance.Color(img).enhance(1.6)
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Contrast(img).enhance(0.9)
        r, g, b = img.split()
        r = ImageEnhance.Brightness(r).enhance(1.08)
        g = ImageEnhance.Brightness(g).enhance(1.03)
        img = Image.merge('RGB', (r, g, b))
        img = img.filter(ImageFilter.SMOOTH)
    elif style == 'comic':
        if use_cv2:
            arr = np.array(img)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            blur = cv2.medianBlur(gray, 7)
            edges = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 9)
            color = cv2.bilateralFilter(arr, 9, 300, 300)
            color = (color // 48) * 48
            edges_rgb = cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)
            result = cv2.bitwise_and(color, edges_rgb)
            img = Image.fromarray(result.astype(np.uint8))
        else:
            img = img.filter(ImageFilter.SMOOTH_MORE)
            img = ImageEnhance.Color(img).enhance(2.0)
            img = ImageEnhance.Contrast(img).enhance(1.8)
        img = ImageEnhance.Color(img).enhance(1.5)
    elif style == 'watercolor':
        img = img.filter(ImageFilter.SMOOTH_MORE)
        img = img.filter(ImageFilter.SMOOTH_MORE)
        img = img.filter(ImageFilter.GaussianBlur(1))
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Color(img).enhance(0.75)
        img = ImageEnhance.Contrast(img).enhance(0.85)
        arr = np.array(img, dtype=np.float32)
        noise = np.random.normal(0, 6, arr.shape)
        arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
    elif style == 'oil':
        img = img.filter(ImageFilter.GaussianBlur(2))
        img = ImageEnhance.Color(img).enhance(1.8)
        img = ImageEnhance.Contrast(img).enhance(1.3)
        img = img.filter(ImageFilter.EDGE_ENHANCE)
        if use_cv2:
            arr = np.array(img)
            arr = cv2.bilateralFilter(arr, 15, 80, 80)
            img = Image.fromarray(arr)
        img = ImageEnhance.Sharpness(img).enhance(1.5)
    elif style == 'scifi':
        img = ImageEnhance.Contrast(img).enhance(1.6)
        img = ImageEnhance.Sharpness(img).enhance(2.5)
        r, g, b = img.split()
        r = ImageEnhance.Brightness(r).enhance(0.75)
        g = ImageEnhance.Brightness(g).enhance(0.9)
        b = ImageEnhance.Brightness(b).enhance(1.35)
        img = Image.merge('RGB', (r, g, b))
        img = ImageEnhance.Color(img).enhance(1.4)
        img = img.filter(ImageFilter.EDGE_ENHANCE_MORE)
    else:
        img = ImageEnhance.Sharpness(img).enhance(1.5)
        img = ImageEnhance.Contrast(img).enhance(1.1)

    return optimize_image_for_transfer(img, max_size=600, quality=75)

def diffusion_generate(image_data_url, style):
    """用 AI Horde (Stable Diffusion) 排隊生成圖片，徹底解決 429 問題"""
    header, b64 = image_data_url.split(',', 1)
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    
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

    submit_url = "https://aihorde.net/api/v2/generate/async"
    req_headers = {
        "apikey": "0000000000", 
        "Content-Type": "application/json",
        "Client-Agent": "pictureguess:1.0",
    }
    
    payload = {
        "prompt": prompt + " ### " + NEGATIVE_PROMPT,
        "params": {
            "width": 512, "height": 512, "steps": 20, "sampler_name": "k_euler_a", "n": 1,
        },
        "nsfw": False,
        "censor_nsfw": True,
        "source_image": source_image_base64, 
        "source_processing": "img2img",
    }
    
    resp = http_requests.post(submit_url, headers=req_headers, json=payload, timeout=30)
    if resp.status_code != 202:
        raise Exception(f"提交給 AI Horde 失敗，伺服器忙碌中。")
    
    job_id = resp.json().get("id")

    check_url  = f"https://aihorde.net/api/v2/generate/check/{job_id}"
    status_url = f"https://aihorde.net/api/v2/generate/status/{job_id}"
    
    for i in range(25): # 最多等 75 秒
        time.sleep(3)
        try:
            check = http_requests.get(check_url, headers=req_headers, timeout=10).json()
        except Exception: continue
        if check.get("faulted"): raise Exception("AI 繪圖任務失敗，請重試。")
        if check.get("done"): break
    else:
        raise Exception("排隊人數較多，生成逾時，請再試一次。")

    result = http_requests.get(status_url, headers=req_headers, timeout=30).json()
    generations = result.get("generations", [])
    if not generations: raise Exception("未取得生成結果。")

    img_data = generations[0].get("img", "")
    if img_data.startswith("http"):
        img_resp = http_requests.get(img_data, timeout=30)
        raw_result = img_resp.content
    else:
        if "," in img_data: img_data = img_data.split(",", 1)[1]
        raw_result = base64.b64decode(img_data)

    result_img = Image.open(io.BytesIO(raw_result)).convert('RGB')
    out_buf = io.BytesIO()
    result_img.save(out_buf, format='PNG')
    
    return "data:image/png;base64," + base64.b64encode(out_buf.getvalue()).decode()

def get_uid_by_sid(sid):
    for uid, p in game_state['participants'].items():
        if p['sid'] == sid: return uid
    return None

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
        'participants': [{'sid': v['sid'], 'name': v['name'], 'score': v['score'], 'online': v.get('online', True)} for v in game_state['participants'].values()],
    })

@app.route('/api/upload_questions', methods=['POST'])
def upload_questions():
    data = request.json
    game_state['questions'] = data.get('questions', [])
    return jsonify({'success': True, 'count': len(game_state['questions'])})

@app.route('/api/diffusion', methods=['POST'])
def api_diffusion():
    data = request.json
    try:
        result = diffusion_generate(data.get('image'), data.get('style', 'realistic'))
        game_state['ai_image'] = result
        game_state['ai_style'] = data.get('style')
        q = game_state['questions'][game_state['current_question']] if game_state['questions'] else {}
        socketio.emit('ai_image_generated', {'image': result, 'style': data.get('style'), 'description': q.get('description', '')})
        return jsonify({'success': True, 'image': result})
    except Exception as e:
        error_msg = str(e)
        return jsonify({'error': error_msg}), 500

@app.route('/api/reset', methods=['POST'])
def api_reset():
    game_state.update({'status': 'waiting', 'questions': [], 'current_question': 0, 'participants': {}, 'answers': {}, 'canvas_data': None, 'ai_image': None})
    socketio.emit('game_reset', {})
    return jsonify({'success': True})

@socketio.on('connect')
def on_connect():
    emit('game_state', {
        'status': game_state['status'],
        'participants': [{'sid': v['sid'], 'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()],
        'current_question': game_state['current_question'],
        'total_questions': len(game_state['questions']),
    })

@socketio.on('disconnect')
def on_disconnect():
    uid = get_uid_by_sid(request.sid)
    if uid: 
        game_state['participants'][uid]['online'] = False
        if game_state['status'] == 'answering':
            online_p = [v for v in game_state['participants'].values() if v.get('online', True)]
            answered_count = sum(1 for v in online_p if v['answered'])
            total_count = len(online_p)
            socketio.emit('answer_progress', {'answered': answered_count, 'total': total_count})
            if answered_count >= total_count and total_count > 0:
                _auto_show_result()

@socketio.on('join_game')
def on_join_game(data):
    number = data.get('number', '').strip()
    sid = request.sid
    if not re.match(r'^(0[1-9]|1[0-9])$', number):
        emit('join_error', {'message': '請輸入 01–19 之間的數字'}); return
    
    uid = number
    name = number + '桌'
    
    if uid in game_state['participants']:
        game_state['participants'][uid]['sid'] = sid
        game_state['participants'][uid]['online'] = True
    else:
        if len(game_state['participants']) >= MAX_PARTICIPANTS:
            emit('join_error', {'message': '已達人數上限'}); return
        game_state['participants'][uid] = {'sid': sid, 'name': name, 'score': 0, 'streak': 0, 'answered': False, 'online': True}
    
    plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()]
    
    payload = {
        'name': name,
        'status': game_state['status'],
        'answered': game_state['participants'][uid]['answered']
    }
    
    if game_state['status'] in ['question', 'answering', 'result'] and game_state['questions'] and game_state['current_question'] < len(game_state['questions']):
        q = _current_question()
        payload['question'] = {
            'index': game_state['current_question'],
            'total': len(game_state['questions']),
            'description': q['description'],
            'options': q['options']
        }

    emit('join_success', payload)
    socketio.emit('participant_joined', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('request_rename')
def on_request_rename():
    uid = get_uid_by_sid(request.sid)
    if not uid: return
    if game_state['status'] != 'waiting':
        emit('rename_error', {'message': '遊戲已開始，無法重新輸入'}); return
    
    name = game_state['participants'][uid]['name']
    del game_state['participants'][uid]
    plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()]
    emit('rename_ok', {})
    socketio.emit('participant_left', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('admin_kick')
def on_admin_kick(data):
    target_name = data.get('name', '')
    target_uid = next((k for k, v in game_state['participants'].items() if v['name'] == target_name), None)
    if target_uid:
        target_sid = game_state['participants'][target_uid]['sid']
        del game_state['participants'][target_uid]
        plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()]
        socketio.emit('participant_left', {'name': target_name, 'participants': plist, 'count': len(plist)})
        socketio.emit('kicked', {}, to=target_sid)

@socketio.on('admin_start_game')
def on_admin_start_game():
    if not game_state['questions']: emit('admin_error', {'message': '請先設定題目'}); return
    game_state['status'] = 'started'
    game_state['current_question'] = 0
    for v in game_state['participants'].values():
        v['score'] = 0; v['streak'] = 0
    _show_question()

@socketio.on('admin_open_answer')
def on_admin_open_answer():
    game_state['status'] = 'answering'
    for v in game_state['participants'].values(): v['answered'] = False
    q = _current_question()
    socketio.emit('start_answering', {
        'image': game_state.get('ai_image') or game_state.get('canvas_data'),
        'canvas_image': game_state.get('canvas_data'),
        'ai_image': game_state.get('ai_image'),
        'has_ai': bool(game_state.get('ai_image') and game_state.get('canvas_data')),
        'ai_style': game_state.get('ai_style'),
        'options': q['options'],
        'description': q['description'],
        'duration': 20,
    })

@socketio.on('admin_switch_image')
def on_admin_switch_image(data):
    mode = data.get('mode', 'ai')
    socketio.emit('switch_image', {
        'image': game_state.get('ai_image') if mode == 'ai' else game_state.get('canvas_data'),
        'mode': mode,
        'ai_style': game_state.get('ai_style') if mode == 'ai' else None,
    })

@socketio.on('admin_next_question')
def on_admin_next_question():
    game_state['current_question'] += 1
    game_state['ai_image'] = game_state['ai_style'] = game_state['canvas_data'] = None
    socketio.emit('clear_canvas')
    if game_state['current_question'] >= len(game_state['questions']):
        _finish_game()
    else:
        _show_question()

@socketio.on('admin_show_rank_points')
def on_admin_show_rank_points():
    ranking = _get_ranking()
    total = len(ranking)
    rank_data = []
    for i, r in enumerate(ranking):
        pts = total - i 
        rank_data.append({'name': r['name'], 'score': pts, 'original_score': r['score']})
    socketio.emit('show_rank_points_screen', {'ranking': rank_data})

@socketio.on('drawing_update')
def on_drawing_update(data):
    if time.time() - game_state.get('last_canvas_update', 0) < CANVAS_UPDATE_THROTTLE:
        game_state['canvas_data'] = data.get('image'); return
    game_state['last_canvas_update'] = time.time()
    game_state['canvas_data'] = data.get('image')
    socketio.emit('canvas_update', {'image': data.get('image')}, broadcast=True)

@socketio.on('submit_answer')
def on_submit_answer(data):
    uid = get_uid_by_sid(request.sid)
    if not uid or game_state['status'] != 'answering' or game_state['participants'][uid]['answered']: return

    p = game_state['participants'][uid]
    p['answered'] = True
    q = _current_question()
    correct = (data.get('answer') == q['correct'])
    
    base = 100 if correct else 0
    time_bonus = max(0, int((20 - data.get('time_taken', 20)) * 5)) if correct else 0
    p['streak'] = p['streak'] + 1 if correct else 0
    streak_bonus = p['streak'] * 10 if correct else 0
    
    total_gain = base + time_bonus + streak_bonus
    p['score'] += total_gain

    ranking = _get_ranking()
    my_rank = next((i+1 for i, r in enumerate(ranking) if r['name'] == p['name']), 0)

    emit('answer_result', {
        'correct': correct, 'correct_answer': q['correct'], 'base_score': base,
        'time_bonus': time_bonus, 'streak_bonus': streak_bonus, 'total_gain': total_gain,
        'total_score': p['score'], 'rank': my_rank, 'streak': p['streak'],
    })

    online_p = [v for v in game_state['participants'].values() if v.get('online', True)]
    answered_count = sum(1 for v in online_p if v['answered'])
    total_count = len(online_p)
    
    socketio.emit('answer_progress', {'answered': answered_count, 'total': total_count})
    if answered_count >= total_count and total_count > 0:
        _auto_show_result()

@socketio.on('admin_show_result')
def on_admin_show_result(): _auto_show_result()

@socketio.on('timer_ended')
def on_timer_ended(): _auto_show_result()

def _current_question(): return game_state['questions'][game_state['current_question']]
def _get_ranking(): return sorted([{'sid': v['sid'], 'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()], key=lambda x: x['score'], reverse=True)

def _auto_show_result():
    if game_state['status'] != 'answering': return
    game_state['status'] = 'result'
    q = _current_question()
    socketio.emit('show_result', {'correct_answer': q['correct'], 'correct_text': q['options'][ord(q['correct']) - ord('A')], 'ranking': _get_ranking()[:10]})

def _show_question():
    game_state['status'] = 'question'
    q = _current_question()
    socketio.emit('show_question', {'index': game_state['current_question'], 'total': len(game_state['questions']), 'description': q['description'], 'options': q['options']})

def _finish_game():
    game_state['status'] = 'finished'
    socketio.emit('game_finished', {'ranking': _get_ranking()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print("\n" + "="*55)
    print("🚀 遊戲伺服器已成功啟動！請點擊以下連結進行測試：")
    print(f"📺 1. 大螢幕展示畫面 : http://127.0.0.1:{port}/show")
    print(f"🛠️ 2. 管理員控制台   : http://127.0.0.1:{port}/admin")
    print(f"📱 3. 玩家答題畫面   : http://127.0.0.1:{port}/game")
    print("-" * 55)
    print("💡 區域網路測試提醒：")
    print("如果想用真實的手機測試，請將上方網址中的 '127.0.0.1' ")
    print("替換成你這台電腦的區域網路 IP (通常是 192.168.X.X)")
    print("="*55 + "\n")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)