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

# 確保 Gemini API 有被正確引入
from google import genai 

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
    'participants': {}, # 現在改為使用 uid (例如 '01', '05') 作為 key
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
    # (保留原有的濾鏡處理作為備用，此處省略以節省篇幅，請保留你原本的 process_image 內容)
    pass 

def diffusion_generate(image_data_url, style, hf_token=None):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: raise Exception("伺服器未設定 GEMINI_API_KEY 環境變數。")

    header, b64 = image_data_url.split(',', 1)
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    
    STYLE_PROMPTS = {
        'realistic': 'Transform this sketch into a high quality, photorealistic image. Keep the core subject and outline exactly the same, but add realistic textures, lighting, and colorful details.',
        'ghibli': 'Transform this sketch into a Studio Ghibli anime style masterpiece. Keep the original outline but add soft lighting, vibrant colors, and beautiful anime shading.',
        'watercolor': 'Transform this sketch into a beautiful watercolor painting. Use soft, transparent washes of color while strictly preserving the original drawing.',
        'comic': 'Transform this sketch into a colorful manga comic style. Enhance it with bold lines and dynamic comic book coloring, but keep the original shape.',
        'oil': 'Transform this sketch into a classic oil painting with thick brushstrokes, rich vivid colors, maintaining the original subject.',
        'scifi': 'Transform this sketch into sci-fi concept art. Add neon colors, futuristic glowing edges, and high contrast while keeping the drawn subject.'
    }
    prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS['realistic'])

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model="gemini-2.5-flash-image", contents=[img, prompt])

    for part in response.parts:
        if part.inline_data is not None:
            generated_img = part.as_image()
            out_buf = io.BytesIO()
            generated_img.save(out_buf, format='PNG')
            return "data:image/png;base64," + base64.b64encode(out_buf.getvalue()).decode()
            
    raise Exception("Gemini 尚未成功回傳圖片，請稍後再試。")

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
        'participants': [{'sid': v['sid'], 'name': v['name'], 'score': v['score'], 'online': v['online']} for v in game_state['participants'].values()],
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
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            return jsonify({'error': '🤖 AI 畫家需要喘口氣！呼叫太頻繁了，請等待約 1 分鐘後再試一次。'}), 429
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
    # 斷線時不刪除資料，只標記為離線 (允許重整網頁)
    if uid: game_state['participants'][uid]['online'] = False

@socketio.on('join_game')
def on_join_game(data):
    number = data.get('number', '').strip()
    sid = request.sid
    if not re.match(r'^(0[1-9]|1[0-9])$', number):
        emit('join_error', {'message': '請輸入 01–19 之間的數字'}); return
    
    uid = number
    name = number + '桌'
    
    # 斷線重連或覆蓋登入
    if uid in game_state['participants']:
        game_state['participants'][uid]['sid'] = sid
        game_state['participants'][uid]['online'] = True
    else:
        if len(game_state['participants']) >= MAX_PARTICIPANTS:
            emit('join_error', {'message': '已達人數上限'}); return
        game_state['participants'][uid] = {'sid': sid, 'name': name, 'score': 0, 'streak': 0, 'answered': False, 'online': True}
    
    plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()]
    emit('join_success', {'name': name, 'status': game_state['status']})
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

# 👇 新增：結算名次積分的邏輯
@socketio.on('admin_show_rank_points')
def on_admin_show_rank_points():
    ranking = _get_ranking()
    total = len(ranking)
    rank_data = []
    for i, r in enumerate(ranking):
        pts = total - i # 第1名得總人數分(最高19)，最後一名得1分
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

    answered_count = sum(1 for v in game_state['participants'].values() if v['answered'])
    socketio.emit('answer_progress', {'answered': answered_count, 'total': len(game_state['participants'])})
    if answered_count >= len(game_state['participants']) and len(game_state['participants']) > 0:
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
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)