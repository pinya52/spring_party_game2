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
    'answer_start_time': 0, # 新增起始時間
    'answer_duration': 20   # 新增總時間長度
}

MAX_PARTICIPANTS = 20
MAX_QUESTIONS = 50

def optimize_image_for_transfer(img, max_size=600, quality=75):
    if img.width > max_size or img.height > max_size:
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def process_image(image_data_url, style):
    pass # 備用濾鏡

def diffusion_generate(image_data_url, style):
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
    
    for i in range(25):
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

def _get_full_ranking():
    current_players = {v['name']: v['score'] for v in game_state['participants'].values()}
    full_list = []
    
    for i in range(1, 20):
        name = f"{i:02d}桌"
        score = current_players.get(name, 0)
        full_list.append({'name': name, 'score': score})
        
    return sorted(full_list, key=lambda x: (-x['score'], x['name']))

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

# 👇 新增：給手機端短輪詢 (Polling) 防漏接收的 API
@app.route('/api/get-game-state')
def api_get_game_state():
    # 💡 1. 取得前端傳來的桌號
    uid = request.args.get('uid') 
    
    q_data = None
    if game_state['status'] in ['question', 'answering', 'result'] and game_state['questions'] and game_state['current_question'] < len(game_state['questions']):
        q = _current_question()
        q_data = {
            'index': game_state['current_question'],
            'total': len(game_state['questions']),
            'description': q['description'],
            'options': q['options'],
            'correct': q['correct'] if game_state['status'] == 'result' else None
        }
    
    # 計算剩餘時間
    rem = 0
    if game_state['status'] == 'answering':
        rem = max(0, int(game_state.get('answer_duration', 20) - (time.time() - game_state.get('answer_start_time', time.time()))))

    # 💡 2. 判斷該玩家是否已經答題
    personal_answered = False
    last_receipt = None
    if uid and uid in game_state['participants']:
        p = game_state['participants'][uid]
        personal_answered = p.get('answered', False)
        last_receipt = p.get('last_receipt') # 💡 終極防禦 2：取出備份收據

    return jsonify({
        'phase': game_state['status'],
        'personal_answered': personal_answered, 
        'last_receipt': last_receipt, # 💡 終極防禦 3：將收據一併傳給手機
        'players': _get_full_ranking(),
        'question': q_data,
        'remaining_time': rem,
        'duration': game_state.get('answer_duration', 20)
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
            # 💡修改：不再過濾 online，而是等待所有有登入過的玩家
            joined_p = game_state['participants'].values()
            answered_count = sum(1 for v in joined_p if v['answered'])
            total_count = len(joined_p)
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
        # 💡新增：防重複登入機制
        # 檢查該桌號是否已經存在，且目前為「上線狀態」
        if game_state['participants'][uid].get('online', False):
            emit('join_error', {'message': f'此桌號 ({name}) 已有人使用，請重新輸入'})
            return
            
        # 如果是離線狀態（例如玩家剛好重新整理網頁），則允許重新接管該桌號
        game_state['participants'][uid]['sid'] = sid
        game_state['participants'][uid]['online'] = True
    else:
        if len(game_state['participants']) >= MAX_PARTICIPANTS:
            emit('join_error', {'message': '已達人數上限'}); return
        game_state['participants'][uid] = {'sid': sid, 'name': name, 'score': 0, 'streak': 0, 'answered': False, 'online': True}
    
    plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score'], 'online': v.get('online', True)} for v in game_state['participants'].values()]
    
    payload = {
        'name': name,
        'status': game_state['status'],
        'answered': game_state['participants'][uid]['answered']
    }
    
    # 傳送剩餘時間給手機端
    if game_state['status'] == 'answering':
        payload['remaining_time'] = max(0, int(game_state.get('answer_duration', 20) - (time.time() - game_state.get('answer_start_time', time.time()))))
        payload['duration'] = game_state.get('answer_duration', 20)

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

    if game_state['status'] == 'result':
        q = _current_question()
        emit('show_result', {
            'correct_answer': q['correct'],
            'correct_text': q['options'][ord(q['correct']) - ord('A')],
            'ranking': _get_full_ranking()
        })
    elif game_state['status'] == 'finished':
        emit('game_finished', {'ranking': _get_full_ranking()})

@socketio.on('request_rename')
def on_request_rename():
    uid = get_uid_by_sid(request.sid)
    if not uid: return
    if game_state['status'] != 'waiting':
        emit('rename_error', {'message': '遊戲已開始，無法重新輸入'}); return
    
    name = game_state['participants'][uid]['name']
    del game_state['participants'][uid]
    plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score'], 'online': v.get('online', True)} for v in game_state['participants'].values()]
    emit('rename_ok', {})
    socketio.emit('participant_left', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('admin_kick')
def on_admin_kick(data):
    target_name = data.get('name', '')
    target_uid = next((k for k, v in game_state['participants'].items() if v['name'] == target_name), None)
    if target_uid:
        target_sid = game_state['participants'][target_uid]['sid']
        del game_state['participants'][target_uid]
        plist = [{'sid': v['sid'], 'name': v['name'], 'score': v['score'], 'online': v.get('online', True)} for v in game_state['participants'].values()]
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
    game_state['answer_start_time'] = time.time()  
    game_state['answer_duration'] = 20             
    for v in game_state['participants'].values(): v['answered'] = False
    q = _current_question()
    
    # 💡恢復傳送圖片資料，因為只會廣播一次，不會造成伺服器負擔
    socketio.emit('start_answering', {
        'image': game_state.get('ai_image') or game_state.get('canvas_data'),
        'canvas_image': game_state.get('canvas_data'),
        'ai_image': game_state.get('ai_image'),
        'has_ai': bool(game_state.get('ai_image') and game_state.get('canvas_data')),
        'ai_style': game_state.get('ai_style'),
        'options': q['options'],
        'description': q['description'],
        'duration': game_state.get('answer_duration', 20),
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

# 👇 修正排名發送，傳送原始分數給前端處理同分邏輯
@socketio.on('admin_show_rank_points')
def on_admin_show_rank_points():
    full_ranking = _get_full_ranking()
    joined_names = [v['name'] for v in game_state['participants'].values()]
    rank_data = []
    
    for r in full_ranking:
        # 將原始分數以及是否有加入遊戲的資訊傳送給前端
        rank_data.append({'name': r['name'], 'score': r['score'], 'joined': r['name'] in joined_names})
        
    socketio.emit('show_rank_points_screen', {'ranking': rank_data})

@socketio.on('drawing_update')
def on_drawing_update(data):
    game_state['canvas_data'] = data.get('image')
    game_state['drawing_active'] = True
    socketio.emit('canvas_update', {'image': data.get('image')})

@socketio.on('submit_answer')
def on_submit_answer(data):
    uid = data.get('uid')
    if not uid:
        uid = next((k for k, v in game_state['participants'].items() if v['sid'] == request.sid), None)
        
    if not uid or uid not in game_state['participants'] or game_state['status'] != 'answering':
        return

    p = game_state['participants'][uid]
    
    # 💡 關鍵防呆：如果這個人這回合已經答過題了，直接拒絕！不給改答案、不給重複計分！
    if p.get('answered', False):
        return
        
    p['answered'] = True
    q = _current_question()
    correct = (data.get('answer') == q['correct'])
    
    # 計算分數邏輯
    base = 100 if correct else 0
    time_bonus = max(0, int((20 - data.get('time_taken', 20)) * 5)) if correct else 0
    
    # 💡 修正連續獎勵邏輯
    if correct:
        # 答對了，連續次數 +1，並給予獎勵
        p['streak'] = p.get('streak', 0) + 1
        streak_bonus = (p['streak']-1) * 10
    else:
        # 答錯了，連續次數立即歸零
        p['streak'] = 0
        streak_bonus = 0
    
    total_gain = base + time_bonus + streak_bonus
    p['score'] += total_gain

    ranking = _get_full_ranking()
    my_rank = next((i+1 for i, r in enumerate(ranking) if r['name'] == p['name']), 0)

    # 💡 終極防禦 1：把這題的「得分收據」備份存進後台該玩家的名單裡！
    receipt = {
        'correct': correct, 'correct_answer': q['correct'], 'base_score': base,
        'time_bonus': time_bonus, 'streak_bonus': streak_bonus, 'total_gain': total_gain,
        'total_score': p['score'], 'rank': my_rank, 'streak': p['streak'],
    }
    p['last_receipt'] = receipt

    emit('answer_result', receipt)

    joined_p = game_state['participants'].values()
    answered_count = sum(1 for v in joined_p if v['answered'])
    total_count = len(joined_p)
    
    socketio.emit('answer_progress', {'answered': answered_count, 'total': total_count})
    if answered_count >= total_count and total_count > 0:
        _auto_show_result()

@socketio.on('admin_show_result')
def on_admin_show_result(): _auto_show_result()

@socketio.on('timer_ended')
def on_timer_ended(): _auto_show_result()

def _current_question(): return game_state['questions'][game_state['current_question']]

def _auto_show_result():
    if game_state['status'] != 'answering': return
    game_state['status'] = 'result'
    q = _current_question()
    socketio.emit('show_result', {'correct_answer': q['correct'], 'correct_text': q['options'][ord(q['correct']) - ord('A')], 'ranking': _get_full_ranking()})

def _show_question():
    game_state['status'] = 'question'
    q = _current_question()
    socketio.emit('show_question', {'index': game_state['current_question'], 'total': len(game_state['questions']), 'description': q['description'], 'options': q['options']})

def _finish_game():
    game_state['status'] = 'finished'
    socketio.emit('game_finished', {'ranking': _get_full_ranking()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print("\n" + "="*55)
    print("🚀 遊戲伺服器已成功啟動！請點擊以下連結進行測試：")
    print(f"📺 1. 大螢幕展示畫面 : http://127.0.0.1:{port}/show")
    print(f"🛠️ 2. 管理員控制台   : http://127.0.0.1:{port}/admin")
    print(f"📱 3. 玩家答題畫面   : http://127.0.0.1:{port}/game")
    print("="*55 + "\n")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)