import os
import base64
import io
import re
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import numpy as np

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'pictureguess-secret-2024')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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

    if style == 'enhance':
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        img = ImageEnhance.Contrast(img).enhance(1.3)
        img = img.filter(ImageFilter.EDGE_ENHANCE)
    elif style == 'cartoon':
        if use_cv2:
            arr = np.array(img)
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
            blur = cv2.medianBlur(gray, 5)
            edges = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 9)
            color = cv2.bilateralFilter(arr, 9, 300, 300)
            edges_rgb = cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)
            img = Image.fromarray(cv2.bitwise_and(color, edges_rgb))
        else:
            img = ImageEnhance.Color(img.filter(ImageFilter.SMOOTH_MORE)).enhance(1.8)
    elif style == 'oil':
        img = ImageEnhance.Contrast(ImageEnhance.Color(img.filter(ImageFilter.GaussianBlur(2))).enhance(1.5)).enhance(1.2)
    elif style == 'watercolor':
        img = ImageEnhance.Color(ImageEnhance.Brightness(img.filter(ImageFilter.SMOOTH_MORE).filter(ImageFilter.SMOOTH_MORE)).enhance(1.1)).enhance(0.8)
    elif style == 'sketch':
        gray = img.convert('L')
        blur = ImageOps.invert(gray).filter(ImageFilter.GaussianBlur(21))
        arr_gray = np.array(gray, dtype=float)
        arr_blur = np.array(blur, dtype=float)
        with np.errstate(divide='ignore', invalid='ignore'):
            result = np.where(arr_blur == 0, 0, (arr_gray / arr_blur) * 255)
        img = Image.fromarray(np.clip(result, 0, 255).astype(np.uint8)).convert('RGB')
    elif style == 'anime':
        if use_cv2:
            arr = np.array(img)
            smooth = cv2.bilateralFilter(arr, 9, 75, 75)
            gray = cv2.cvtColor(smooth, cv2.COLOR_RGB2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            img = Image.fromarray(cv2.subtract(smooth, cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)))
        img = ImageEnhance.Color(img).enhance(2.0)
    elif style == 'popart':
        img = ImageEnhance.Color(ImageEnhance.Contrast(img).enhance(2.0)).enhance(2.0)
        if use_cv2:
            arr = np.array(img)
            img = Image.fromarray(((arr // 64) * 64).astype(np.uint8))

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

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
        'participants': [{'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()],
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
    style = data.get('style', 'enhance')
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
        return jsonify({'error': str(e)}), 500

@socketio.on('connect')
def on_connect():
    emit('game_state', {
        'status': game_state['status'],
        'participants': [{'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()],
        'current_question': game_state['current_question'],
        'total_questions': len(game_state['questions']),
    })

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in game_state['participants']:
        name = game_state['participants'][sid]['name']
        del game_state['participants'][sid]
        plist = [{'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()]
        socketio.emit('participant_left', {'name': name, 'participants': plist, 'count': len(plist)})

@socketio.on('join_game')
def on_join_game(data):
    name = data.get('name', '').strip()
    sid = request.sid
    if not name:
        emit('join_error', {'message': '請輸入組別名稱'}); return
    if len(name) > 20:
        emit('join_error', {'message': '名稱不可超過 20 字元'}); return
    if not re.match(r'^[\u4e00-\u9fff\w\s\-_]+$', name):
        emit('join_error', {'message': '名稱只允許中文、英文、數字、空格、連字號、底線'}); return
    existing = [v['name'] for v in game_state['participants'].values()]
    if name in existing:
        emit('join_error', {'message': '此名稱已被使用'}); return
    if len(game_state['participants']) >= MAX_PARTICIPANTS:
        emit('join_error', {'message': '已達人數上限 {} 組'.format(MAX_PARTICIPANTS)}); return
    game_state['participants'][sid] = {'name': name, 'score': 0, 'streak': 0, 'answered': False}
    plist = [{'name': v['name'], 'score': v['score']} for v in game_state['participants'].values()]
    emit('join_success', {'name': name, 'status': game_state['status']})
    socketio.emit('participant_joined', {'name': name, 'participants': plist, 'count': len(plist)})

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

# ── timer ended on client, force show result ──
@socketio.on('timer_ended')
def on_timer_ended():
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