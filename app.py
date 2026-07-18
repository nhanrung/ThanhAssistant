import os
import requests
import base64
import json
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

app = Flask(__name__, template_folder='.')
CORS(app)

# --- CẤU HÌNH ĐỒNG BỘ GITHUB (BẮT BUỘC) ---
GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" # Thay bằng Token của bạn
GITHUB_REPO = "username/ten-kho-luu-tru" # Thay bằng username/ten-repo của bạn
GITHUB_FILE_PATH = "learning_log.json" # Tên file JSON lưu trên GitHub
GITHUB_BRANCH = "main" # Hoặc master tùy repo của bạn

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6IFc6oXzX0QEpZU5D-hpkEEUnpuhttIW24wopkYjVX19A")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

# Hàm helper lấy file từ GitHub
def get_file_from_github():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}?ref={GITHUB_BRANCH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return json.loads(content), data['sha']
    elif response.status_code == 404:
        return [], None # File chưa tồn tại
    return None, None

# Hàm helper ghi đè file lên GitHub
def save_file_to_github(data_list, sha=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    content_bytes = json.dumps(data_list, ensure_ascii=False, indent=4).encode('utf-8')
    content_base64 = base64.b64encode(content_bytes).decode('utf-8')
    
    payload = {
        "message": "🔄 Auto-update learning log via AI English App",
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

# API lấy toàn bộ bài học từ GitHub về thiết bị khi vừa đăng nhập
@app.route('/api/get-logs', methods=['GET'])
def get_logs():
    logs, _ = get_file_from_github()
    if logs is str or logs is None:
        return jsonify({"status": "error", "message": "Không thể kết nối GitHub"}), 500
    return jsonify({"status": "success", "data": logs})

# API lưu bài học mới lên GitHub
@app.route('/api/save-log', methods=['POST'])
def save_log():
    new_entry = request.json
    logs, sha = get_file_from_github()
    if logs is None:
        logs = []
    
    logs.append(new_entry) # Thêm bài học mới vào danh sách
    
    if save_file_to_github(logs, sha):
        return jsonify({"status": "success", "data": logs})
    return jsonify({"status": "error", "message": "Lưu lên GitHub thất bại"}), 500

# API xóa bài học khỏi GitHub
@app.route('/api/delete-log', methods=['POST'])
def delete_log_api():
    index_to_delete = request.json.get('index')
    logs, sha = get_file_from_github()
    if logs and 0 <= index_to_delete < len(logs):
        logs.pop(index_to_delete)
        if save_file_to_github(logs, sha):
            return jsonify({"status": "success", "data": logs})
    return jsonify({"status": "error", "message": "Xóa thất bại"}), 500

# API dịch thuật 3 tầng giữ nguyên
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

    return jsonify({"status": "error", "message": "Lỗi dịch"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)