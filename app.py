import os
import requests
import base64
import json
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

app = Flask(__name__, template_folder='.')
CORS(app)

# --- CẤU HÌNH GITHUB (Lấy từ biến môi trường của Server) ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "nhanrung/ThanhAssistant"  # Tên repo của bạn
GITHUB_BRANCH = "main"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# --- DANH SÁCH TÀI KHOẢN (Giữ nguyên cấu hình cũ của bạn) ---
USER_ACCOUNTS = {
    "thanh": "th@nh341978",
    "lam": "lam123",
    "anh": "anh123"
}

# Hàm đọc file JSON của từng User từ GitHub
def get_user_file_from_github(username):
    filename = f"learning_log_{username}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}?ref={GITHUB_BRANCH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return json.loads(content), data['sha']
    elif response.status_code == 404:
        return [], None  # Nếu user chưa có file, tự tạo mảng rỗng
    return None, None

# Hàm ghi đè file JSON của từng User lên GitHub
def save_user_file_to_github(username, data_list, sha=None):
    filename = f"learning_log_{username}.json"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    content_bytes = json.dumps(data_list, ensure_ascii=False, indent=4).encode('utf-8')
    content_base64 = base64.b64encode(content_bytes).decode('utf-8')
    
    payload = {
        "message": f"🔄 Auto-update log for {username}",
        "content": content_base64,
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha
        
    response = requests.put(url, json=payload, headers=headers)
    return response.status_code in [200, 201]

@app.route('/')
def index():
    return render_template('index.html')

# API Đăng nhập & Lấy dữ liệu riêng của từng User
@app.route('/api/login', methods=['POST'])
def login_api():
    data = request.json or {}
    username = data.get('username', '').lower().strip()
    password = data.get('password', '')
    
    if username in USER_ACCOUNTS and USER_ACCOUNTS[username] == password:
        logs, _ = get_user_file_from_github(username)
        if logs is None:
            return jsonify({"status": "error", "message": "Lỗi kết nối kho dữ liệu GitHub"}), 500
        return jsonify({"status": "success", "username": username, "data": logs})
    
    return jsonify({"status": "error", "message": "Sai tài khoản hoặc mật khẩu!"}), 401

# API Lưu bài học mới theo User
@app.route('/api/save-log', methods=['POST'])
def save_log():
    data = request.json or {}
    username = data.get('username', '').lower().strip()
    new_entry = data.get('entry')
    
    if username not in USER_ACCOUNTS:
        return jsonify({"status": "error", "message": "User không hợp lệ"}), 403
        
    logs, sha = get_user_file_from_github(username)
    if logs is None: logs = []
    
    logs.append(new_entry)
    
    if save_user_file_to_github(username, logs, sha):
        return jsonify({"status": "success", "data": logs})
    return jsonify({"status": "error", "message": "Lưu dữ liệu lên GitHub thất bại"}), 500

# API Xóa bài học theo User
@app.route('/api/delete-log', methods=['POST'])
def delete_log_api():
    data = request.json or {}
    username = data.get('username', '').lower().strip()
    index_to_delete = data.get('index')
    
    if username not in USER_ACCOUNTS:
        return jsonify({"status": "error", "message": "User không hợp lệ"}), 403
        
    logs, sha = get_user_file_from_github(username)
    if logs and 0 <= index_to_delete < len(logs):
        logs.pop(index_to_delete)
        if save_user_file_to_github(username, logs, sha):
            return jsonify({"status": "success", "data": logs})
            
    return jsonify({"status": "error", "message": "Xóa thất bại"}), 500

# API Dịch thuật (Giữ nguyên tính năng dịch 3 tầng của bạn)
@app.route('/api/translate', methods=['POST'])
def translate_api():
    data = request.json or {}
    text = data.get('text', '')
    target_lang = data.get('target_lang', 'vi')
    source_lang = data.get('source_lang', 'en')

    # TẦNG 1: GEMINI
    try:
        prompt = f"Dịch sang tiếng {'Việt' if target_lang=='vi' else 'Anh'} tự nhiên, giữ nguyên định dạng: \"{text}\""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        res = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=5)
        if res.status_code == 200:
            translated = res.json()['candidates'][0]['content']['parts'][0]['text'].replace('"', '').strip()
            return jsonify({"status": "success", "translated": translated})
    except Exception: pass

    # TẦNG 2: GOOGLE TRANSLATE FREE
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={source_lang}&tl={target_lang}&dt=t&q={requests.utils.quote(text)}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            translated = "".join([x[0] for x in res.json()[0]]).strip()
            return jsonify({"status": "success", "translated": translated})
    except Exception: pass

    return jsonify({"status": "error", "message": "Lỗi hệ thống dịch"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
