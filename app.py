import json
import os
import re
import html as html_lib
import base64
import requests
import time
import pytz
import urllib.parse
from datetime import datetime
from flask import Flask, request, jsonify, render_template

try:
    import eng_to_ipa as ipa
    HAS_ENG_TO_IPA = True
except ImportError:
    HAS_ENG_TO_IPA = False

app = Flask(__name__)

# ============================================================
# CẤU HÌNH HỆ THỐNG
# ============================================================
SYSTEM_PASSWORD = "th@nh341978"   # Mật khẩu hệ thống chung (admin dùng khi đăng nhập lớp 1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, 'userdata')
USERS_FILE = os.path.join(DATA_DIR, '_users.json')   # Danh sách user con do admin tạo

# ------------------------------------------------------------
# LƯU TRỮ DỮ LIỆU TRÊN GITHUB (thay cho đĩa cục bộ)
# ------------------------------------------------------------
# LÝ DO: Render (free) và PythonAnywhere (free) đều có đĩa KHÔNG bền
# vững — Render xoá sạch mọi file ghi ra ngoài lúc build mỗi khi service
# "ngủ đông" rồi khởi động lại / deploy lại; PythonAnywiwe free thì giới
# hạn dung lượng và không đồng bộ giữa các lần deploy. Hậu quả: nếu vẫn
# ghi 'userdata/*.json' xuống đĩa cục bộ như cũ, dữ liệu học (thêm/xoá
# câu, đổi mật khẩu, thêm username...) sẽ MẤT sau mỗi lần server khởi
# động lại.
#
# GIẢI PHÁP: Toàn bộ dữ liệu (danh sách user con '_users.json' và từng
# file 'learning_log_<username>.json') được đọc/ghi trực tiếp vào repo
# GitHub này (nhanrung/ThanhAssistant) thông qua GitHub Contents API.
# Nhờ vậy dữ liệu bền vững vĩnh viễn, và dùng chung được dù đang chạy
# trên Render, PythonAnywhere, hay bất kỳ server miễn phí nào khác —
# vì tất cả cùng đọc/ghi về một nơi duy nhất: GitHub.
#
# CẤU HÌNH (đặt qua biến môi trường trên Render / PythonAnywhere):
#   GITHUB_TOKEN  : Personal Access Token có quyền ghi vào repo
#                   (Fine-grained PAT -> chọn đúng repo -> quyền
#                   "Contents: Read and write". Hoặc Classic PAT với
#                   scope "repo").
#   GITHUB_OWNER  : chủ sở hữu repo, mặc định "nhanrung"
#   GITHUB_REPO   : tên repo, mặc định "ThanhAssistant"
#   GITHUB_BRANCH : nhánh dùng để đọc/ghi, mặc định "main"
#
# LƯU Ý AN TOÀN: KHÔNG hard-code GITHUB_TOKEN trong code (khác với các
# API key dịch thuật ở trên vốn đã có sẵn giá trị mặc định) — token này
# có quyền ghi vào repo nên bắt buộc phải đặt qua biến môi trường trên
# Render/PythonAnywhere. Nếu chưa cấu hình, hệ thống tự động dùng lại
# đĩa cục bộ như cũ (chỉ để chạy thử ở máy cá nhân), và sẽ in cảnh báo.
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER  = os.environ.get("GITHUB_OWNER", "nhanrung")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "ThanhAssistant")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_DATA_PATH = "userdata"   # thư mục trong repo chứa các file dữ liệu
GITHUB_API_BASE = "https://api.github.com"

# Bộ nhớ đệm SHA của từng file trong repo (path -> sha). GitHub Contents
# API bắt buộc phải biết đúng "sha" hiện tại của file thì mới cho ghi đè
# (giống git commit), nếu không sẽ báo lỗi 409 (conflict). Nhờ nhớ lại
# sha ngay sau mỗi lần đọc/ghi thành công, ta tránh phải gọi thêm 1 lần
# GET trước mỗi lần PUT -> giảm độ trễ.
_GITHUB_SHA_CACHE = {}


def _github_storage_configured():
    """True nếu đã cấu hình đủ để lưu trữ trên GitHub."""
    return bool(GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO)


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_get_file(repo_path):
    """
    Đọc 1 file từ repo GitHub qua Contents API.
    Trả về dict {"text": <nội dung file dạng str>, "sha": <sha file>}
    hoặc None nếu file chưa tồn tại / có lỗi.
    """
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{repo_path}"
    try:
        resp = requests.get(
            url, headers=_github_headers(),
            params={"ref": GITHUB_BRANCH}, timeout=10
        )
        if resp.status_code == 200:
            payload = resp.json()
            sha = payload.get("sha")
            raw_b64 = (payload.get("content") or "").replace("\n", "")
            text = base64.b64decode(raw_b64).decode("utf-8") if raw_b64 else ""
            if sha:
                _GITHUB_SHA_CACHE[repo_path] = sha
            return {"text": text, "sha": sha}
        elif resp.status_code == 404:
            return None
        else:
            print(f"GitHub GET '{repo_path}' lỗi {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"Lỗi gọi GitHub GET '{repo_path}': {str(e)}")
        return None


def _github_put_file(repo_path, text_content, commit_message,
                      max_retries=3, timeout=30):
    """
    Ghi (tạo mới hoặc cập nhật) 1 file trong repo GitHub qua Contents API.
    Trả về True nếu thành công, False nếu thất bại.

    LỊCH SỬ SỬA LỖI (quan trọng, đọc trước khi sửa lại hàm này):
    Bản trước đây từng CHỦ ĐỘNG tự tính và ép cứng header "Content-Length"
    (serialize JSON thủ công thành bytes rồi gửi qua data=...). Ý định là
    để "an toàn" cho payload lớn, nhưng trên thực tế lại gây ra lỗi MỚI và
    NẶNG HƠN: GitHub trả về "400 - malformed request" ngay cả với file nhỏ
    (ví dụ learning_log_thanh.json) — nhiều khả năng do proxy whitelist
    của PythonAnywiwe free rewrap lại request khi chuyển tiếp, khiến số
    byte thực nhận được lệch với Content-Length đã khai báo cứng, khiến
    GitHub từ chối thẳng request thay vì chỉ timeout âm thầm như trước.
    -> ĐÃ QUAY LẠI dùng json=payload để để "requests" tự lo toàn bộ việc
    serialize + tính Content-Length (đáng tin cậy hơn khi đi qua proxy),
    chỉ giữ lại phần tăng timeout + cơ chế thử lại.
    """
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{repo_path}"
    b64_content = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")

    def _attempt(sha):
        payload = {
            "message": commit_message,
            "content": b64_content,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        headers = _github_headers()
        # Không tự set Content-Type/Content-Length thủ công -> để
        # requests tự tính chính xác theo đúng bytes thật sự gửi đi.
        return requests.put(url, headers=headers, json=payload, timeout=timeout)

    last_network_error = None
    try:
        cached_sha = _GITHUB_SHA_CACHE.get(repo_path)

        for attempt in range(1, max_retries + 1):
            try:
                resp = _attempt(cached_sha)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                # Lỗi nghi do mạng/proxy cắt cụt request -> thử lại.
                last_network_error = e
                print(f"GitHub PUT '{repo_path}' lỗi mạng (lần {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2 ** (attempt - 1))  # backoff: 1s, 2s, 4s...
                    continue
                print(f"GitHub PUT '{repo_path}' thất bại sau {max_retries} lần thử do lỗi mạng.")
                return False

            # sha lưu trong bộ nhớ đệm bị cũ (file đã đổi trên GitHub từ
            # nơi khác) -> đọc lại sha mới nhất rồi thử ghi lại.
            if resp.status_code == 409:
                existing = _github_get_file(repo_path)
                cached_sha = existing["sha"] if existing else None
                try:
                    resp = _attempt(cached_sha)
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.ChunkedEncodingError) as e:
                    last_network_error = e
                    print(f"GitHub PUT '{repo_path}' lỗi mạng sau khi làm mới sha (lần {attempt}/{max_retries}): {e}")
                    if attempt < max_retries:
                        time.sleep(2 ** (attempt - 1))
                        continue
                    return False

            if resp.status_code in (200, 201):
                new_sha = (resp.json().get("content") or {}).get("sha")
                if new_sha:
                    _GITHUB_SHA_CACHE[repo_path] = new_sha
                return True

            # 5xx (lỗi tạm thời phía GitHub/proxy) -> đáng để thử lại.
            if 500 <= resp.status_code < 600:
                print(f"GitHub PUT '{repo_path}' lỗi {resp.status_code} (lần {attempt}/{max_retries}): {resp.text[:300]}")
                if attempt < max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                return False

            # Lỗi 4xx còn lại (sai token, payload không hợp lệ...) -> lỗi
            # logic, retry thêm cũng vô ích, dừng ngay.
            print(f"GitHub PUT '{repo_path}' lỗi {resp.status_code}: {resp.text[:300]}")
            return False

        return False
    except Exception as e:
        print(f"Lỗi gọi GitHub PUT '{repo_path}': {str(e)}")
        return False


if not _github_storage_configured():
    print(
        "[CẢNH BÁO] Chưa cấu hình GITHUB_TOKEN -> dữ liệu học đang được "
        "lưu tạm trên đĩa cục bộ của server. Trên Render/PythonAnywhere "
        "free, dữ liệu sẽ MẤT khi server khởi động lại! Hãy đặt biến môi "
        "trường GITHUB_TOKEN (và GITHUB_OWNER/GITHUB_REPO nếu khác mặc "
        "định) để lưu bền vững trên GitHub."
    )

# ------------------------------------------------------------
# CẤU HÌNH DỊCH THUẬT (Gemini -> Groq -> Google Translate)
# ------------------------------------------------------------
# CHIẾN LƯỢC DỊCH 3 TẦNG (mới):
#   1) Gemini AI (Google) — ưu tiên hàng đầu, chất lượng dịch ngữ cảnh tốt
#      nhất trong 3 lựa chọn hiện có.
#   2) Groq AI (openai/gpt-oss-120b) — dùng khi Gemini bị quá tải / lỗi /
#      hết hạn mức (429, 503, timeout, v.v.)
#   3) Google Translate (free_translate) — phương án cuối cùng, chỉ dùng
#      khi CẢ HAI AI ở trên đều thất bại.
#
# GHI CHÚ CŨ (vẫn còn giá trị cho tầng Groq):
# Model cũ "llama3-8b-8192" đã bị Groq NGỪNG HỖ TRỢ (decommissioned) từ
# 30/08/2025. -> Đổi sang "openai/gpt-oss-120b": model production hiện tại
# của Groq, chất lượng dịch đa ngôn ngữ tốt, KHÔNG nằm trong danh sách
# model sắp bị deprecate.
#
# Nên đặt các API KEY qua biến môi trường khi có thể, thay vì hard-code.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6IFc6oXzX0QEpZU5D-hpkEEUnpuhttIW24wopkYjVX19A")
GEMINI_TRANSLATE_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TRANSLATE_MODEL}:generateContent"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
GROQ_TRANSLATE_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Mã lỗi HTTP coi là "quá tải / hết hạn mức" -> chuyển ngay sang tầng dịch kế tiếp
_OVERLOAD_STATUS_CODES = {429, 500, 502, 503, 504}

# ============================================================
# QUẢN LÝ DANH SÁCH USER CON
# ============================================================

def _local_load_json(filepath, default):
    """Đọc JSON từ đĩa cục bộ — chỉ dùng khi chưa cấu hình GitHub (dự phòng)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _local_save_json(filepath, data):
    """Ghi JSON xuống đĩa cục bộ — chỉ dùng khi chưa cấu hình GitHub (dự phòng)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_users_db():
    """
    Tải danh sách user con.
    Ưu tiên đọc từ file 'userdata/_users.json' trong repo GitHub (bền
    vững trên mọi server). Nếu chưa cấu hình GitHub, đọc từ đĩa cục bộ.
    """
    if _github_storage_configured():
        result = _github_get_file(f"{GITHUB_DATA_PATH}/_users.json")
        if result is None:
            return {}
        try:
            return json.loads(result["text"]) if result["text"].strip() else {}
        except Exception:
            print("Lỗi đọc _users.json từ GitHub (JSON hỏng) -> trả về danh sách rỗng.")
            return {}
    return _local_load_json(USERS_FILE, {})


def save_users_db(users_db):
    """
    Lưu danh sách user con.
    Ưu tiên ghi lên file 'userdata/_users.json' trong repo GitHub. Nếu
    ghi GitHub thất bại (mất mạng, token sai...), tự động ghi tạm xuống
    đĩa cục bộ để không mất thao tác của người dùng ngay lúc đó.
    """
    text = json.dumps(users_db, ensure_ascii=False, indent=4)
    if _github_storage_configured():
        ok = _github_put_file(
            f"{GITHUB_DATA_PATH}/_users.json", text,
            "Cập nhật danh sách tài khoản người học"
        )
        if ok:
            return
        print("[CẢNH BÁO] Ghi _users.json lên GitHub thất bại -> lưu tạm cục bộ.")
    _local_save_json(USERS_FILE, users_db)

def is_admin_user(sys_username):
    """Kiểm tra xem tài khoản có phải admin không (username kết thúc bằng _admin)."""
    return sys_username.strip().lower().endswith('_admin')

# ============================================================
# PHÁT ÂM IPA & TIỆN ÍCH
# ============================================================

OXFORD_3000_DICT = {
    "the": "ðə", "be": "biː", "to": "tuː", "of": "əv", "and": "ənd",
    "a": "ə", "in": "ɪn", "that": "ðæt", "have": "həv", "i": "aɪ",
    "it": "ɪt", "for": "fə", "not": "nɒt", "on": "ɒn", "with": "wɪð",
    "he": "hiː", "as": "əz", "you": "juː", "do": "duː", "at": "æt",
    "this": "ðɪs", "but": "bʌt", "his": "hɪz", "by": "baɪ", "from": "frəm",
    "they": "ðeɪ", "we": "wiː", "say": "seɪ", "her": "hɜː(r)", "she": "ʃiː",
    "or": "ɔː(r)", "an": "ən", "will": "wɪl", "my": "maɪ", "one": "wʌn",
    "all": "ɔːl", "would": "wʊd", "there": "ðeə(r)", "their": "ðeə(r)",
    "what": "wɒt", "so": "səʊ", "up": "ʌp", "out": "aʊt", "if": "ɪf",
    "about": "əˈbaʊt", "who": "huː", "get": "ɡet", "which": "wɪtʃ",
    "go": "ɡəʊ", "me": "miː", "when": "wen", "make": "meɪk", "can": "kæn",
    "like": "laɪk", "time": "taɪm", "no": "nəʊ", "just": "dʒʌst",
    "him": "hɪm", "know": "nəʊ", "take": "teɪk", "people": "ˈpiːpl",
    "into": "ˈɪntuː", "year": "jɪə(r)", "your": "jɔː(r)", "good": "ɡʊd",
    "some": "sʌm", "could": "kʊd", "them": "ðəm", "see": "siː",
    "other": "ˈʌðə(r)", "than": "ðæn", "then": "ðen", "now": "naʊ",
    "look": "lʊk", "only": "ˈəʊnli", "come": "kʌm", "its": "ɪts",
    "over": "ˈəʊvə(r)", "think": "θɪŋk", "also": "ˈɔːlsəʊ", "back": "bæk",
    "after": "ˈɑːftə(r)", "use": "juːz", "two": "tuː", "how": "haʊ",
    "our": "ˈaʊə(r)", "work": "wɜːk", "first": "fɜːst", "well": "wel",
    "way": "weɪ", "even": "ˈiːvn", "new": "njuː", "want": "wɒnt",
    "because": "bɪˈkəz", "any": "ˈeni", "these": "ðiːz", "give": "ɡɪv",
    "day": "deɪ", "most": "məʊst", "us": "əs",
    "conversation": "ˌkɒnvəˈseɪʃn", "conversations": "ˌkɒnvəˈseɪʃnz",
    "next": "nekst", "keep": "kiːp", "going": "ˈɡəʊɪŋ",
    "compliment": "ˈkɒmplɪmənt", "questions": "ˈkwestʃənz", "show": "ʃəʊ",
    "interest": "ˈɪntrəst", "oh": "əʊ", "you've": "juːv", "paris": "ˈpærɪs",
    "don't": "dəʊnt", "talking": "ˈtɔːkɪŋ", "avoid": "əˈvɔɪd",
    "saying": "ˈseɪɪŋ", "anything": "ˈeniθɪŋ", "offensive": "əˈfensɪv",
    "awkward": "ˈɔːkwəd", "create": "kriˈeɪt", "negative": "ˈneɡətɪv",
    "impression": "ɪmˈpreʃn", "possibly": "ˈpɒsəbli", "end": "end"
}

def verify_request_password(req_data_or_args):
    """
    Xác thực request cho các route học tập:
    - Chấp nhận SYSTEM_PASSWORD (admin)
    - Chấp nhận mật khẩu riêng của user con (kèm theo username)
    """
    provided_password = req_data_or_args.get('password', '')
    if provided_password == SYSTEM_PASSWORD:
        return True
    # Kiểm tra mật khẩu user con
    username = req_data_or_args.get('username', '').strip().lower()
    if username:
        users_db = load_users_db()
        if username in users_db:
            return users_db[username].get('password') == provided_password
    return False

def verify_admin_password(req_data_or_args):
    """Chỉ chấp nhận SYSTEM_PASSWORD — dùng cho các route quản trị."""
    provided_password = req_data_or_args.get('password', '')
    return provided_password == SYSTEM_PASSWORD

def load_data(username):
    """
    Tải nhật ký học của 1 người dùng.
    Ưu tiên đọc từ 'userdata/learning_log_<username>.json' trong repo
    GitHub. Nếu chưa cấu hình GitHub, đọc từ đĩa cục bộ.
    """
    uname = username.lower()
    if _github_storage_configured():
        result = _github_get_file(f"{GITHUB_DATA_PATH}/learning_log_{uname}.json")
        if result is None:
            return []
        try:
            return json.loads(result["text"]) if result["text"].strip() else []
        except Exception:
            print(f"Lỗi đọc learning_log_{uname}.json từ GitHub (JSON hỏng) -> trả về danh sách rỗng.")
            return []
    filename = os.path.join(DATA_DIR, f'learning_log_{uname}.json')
    return _local_load_json(filename, [])


def save_data(username, data):
    """
    Lưu nhật ký học của 1 người dùng.
    Ưu tiên ghi lên 'userdata/learning_log_<username>.json' trong repo
    GitHub — đây là bước chạy MỖI KHI người học thêm/sửa/xoá 1 câu. Nếu
    ghi GitHub thất bại, tự động ghi tạm xuống đĩa cục bộ để không mất
    thao tác của người dùng ngay lúc đó.
    """
    uname = username.lower()
    text = json.dumps(data, ensure_ascii=False, indent=4)
    if _github_storage_configured():
        ok = _github_put_file(
            f"{GITHUB_DATA_PATH}/learning_log_{uname}.json", text,
            f"Cập nhật nhật ký học của '{uname}'"
        )
        if ok:
            return
        print(f"[CẢNH BÁO] Ghi learning_log_{uname}.json lên GitHub thất bại -> lưu tạm cục bộ.")
    filename = os.path.join(DATA_DIR, f'learning_log_{uname}.json')
    _local_save_json(filename, data)

def clean_html_for_spellcheck(text):
    """
    Làm sạch HTML trước khi phiên âm / kiểm tra ngữ pháp.
    SỬA LỖI: trước đây các thực thể HTML như '&nbsp;' không được giải mã,
    nên khi nối với chữ liền kề sẽ tạo ra rác kiểu "supertastersnbsp".
    """
    if not text:
        return ""
    cleaned = text.replace("<br>", "\n").replace("<p>", "").replace("</p>", "\n")
    cleaned = re.sub(r'<[^>]+>', '', cleaned)            # bỏ các tag HTML còn lại
    cleaned = html_lib.unescape(cleaned)                  # &nbsp; -> ' ', &amp; -> '&', ...
    cleaned = cleaned.replace('\xa0', ' ')                # khoảng trắng không ngắt dòng -> khoảng trắng thường
    cleaned = re.sub(r'[¹²³⁴⁵⁶⁷⁸⁹⁰]', '', cleaned)       # bỏ số mũ chú thích (footnote)
    return cleaned

# ------------------------------------------------------------
# BỘ PHIÊN ÂM IPA NHIỀU TẦNG (multi-tier IPA resolver)
# ------------------------------------------------------------
# Mục tiêu: hạn chế tối đa việc một từ bị "bỏ rơi" và hiển thị nguyên
# dạng tiếng Anh thay vì phiên âm. Thứ tự ưu tiên cho MỖI từ:
#   1) Từ điển Oxford 3000 đã soạn sẵn (nhanh, chính xác)
#   2) eng_to_ipa (dựa trên từ điển CMU)
#   3) Tách hậu tố (số nhiều, thì quá khứ, -ing, -er, -ly...) rồi ghép
#      đúng âm hậu tố vào gốc từ đã phiên âm được (ví dụ: tasters =
#      taste + er + s -> ghép đúng âm /z/ vì gốc kết thúc bằng nguyên âm)
#   4) Tách tiền tố thông dụng (non-, super-, un-, re-, dis-...) rồi
#      ghép với phần còn lại đã phiên âm được
#   5) Tra từ điển trực tuyến (dictionaryapi.dev) — dùng cho cả hai
#      trường hợp có/không có eng_to_ipa, vì từ điển CMU không có hết
#      mọi từ
#   6) (chỉ khi tất cả đều thất bại) suy đoán âm theo quy tắc đánh vần
#      — không đảm bảo chính xác 100%, chỉ để tránh hiển thị y nguyên
#      chữ tiếng Anh.

COMMON_PREFIXES_IPA = {
    "multi": "ˈmʌlti", "micro": "ˈmaɪkrəʊ", "super": "ˈsuːpə",
    "inter": "ˈɪntə", "under": "ˈʌndə", "anti": "ˈænti",
    "auto": "ˈɔːtəʊ", "over": "ˈəʊvə", "semi": "ˈsemi",
    "mini": "ˈmɪni", "post": "pəʊst", "non": "nɒn",
    "pre": "priː", "dis": "dɪs", "mis": "mɪs", "out": "aʊt",
    "sub": "sʌb", "un": "ʌn", "re": "riː", "co": "kəʊ",
}
_PREFIXES_BY_LENGTH = sorted(COMMON_PREFIXES_IPA.keys(), key=len, reverse=True)


def _direct_lookup(word):
    """Tra trực tiếp: từ điển soạn sẵn -> eng_to_ipa. Trả None nếu không có."""
    if not word:
        return None
    if word in OXFORD_3000_DICT:
        return OXFORD_3000_DICT[word]
    if HAS_ENG_TO_IPA:
        try:
            converted = ipa.convert(word)
            if converted and "*" not in converted:
                return converted
        except Exception:
            pass
    return None


def _dictionary_api_lookup(word):
    """Tra từ điển trực tuyến — tầng dự phòng cuối trước khi suy đoán."""
    try:
        res = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=3)
        if res.status_code == 200:
            data = res.json()
            phonetic = data[0].get("phonetic", "")
            if not phonetic:
                for p in data[0].get("phonetics", []):
                    if p.get("text"):
                        phonetic = p.get("text")
                        break
            if phonetic:
                return phonetic.strip("/")
    except Exception:
        pass
    return None


def _graft_suffix(stem_ipa, suffix_code):
    """Ghép hậu tố đúng âm thay vì chữ cái thô."""
    if suffix_code == "@s":  # số nhiều / động từ số ít ngôi 3 (-s/-es)
        last = stem_ipa[-1] if stem_ipa else ""
        if last in "szʒʃ" or stem_ipa.endswith(("tʃ", "dʒ", "ʤ", "ʧ")):
            return stem_ipa + "ɪz"
        if last in "ptkf" or stem_ipa.endswith("θ"):
            return stem_ipa + "s"
        return stem_ipa + "z"
    if suffix_code == "@ed":  # thì quá khứ (-ed)
        last = stem_ipa[-1] if stem_ipa else ""
        if last in "td":
            return stem_ipa + "ɪd"
        if last in "ptkfsʃ" or stem_ipa.endswith("θ"):
            return stem_ipa + "t"
        return stem_ipa + "d"
    return stem_ipa + suffix_code  # hậu tố cố định (ɪŋ, ər, ɪst, li, nəs, fʊl, ləs, mənt)


def _strip_suffix_and_resolve(word, depth):
    """Thử bỏ các hậu tố tiếng Anh thông dụng rồi phiên âm phần gốc."""
    candidates = []
    if word.endswith("ies") and len(word) > 4:
        candidates.append((word[:-3] + "y", "@s"))           # babies -> baby
    if word.endswith("ied") and len(word) > 4:
        candidates.append((word[:-3] + "y", "@ed"))          # studied -> study
    if word.endswith("es") and len(word) > 3:
        candidates.append((word[:-2], "@s"))
        candidates.append((word[:-2] + "e", "@s"))
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        candidates.append((word[:-1], "@s"))                 # tasters -> taster
    if word.endswith("ed") and len(word) > 3:
        candidates.append((word[:-2], "@ed"))
        candidates.append((word[:-1], "@ed"))                # liked -> like
    if word.endswith("ing") and len(word) > 4:
        candidates.append((word[:-3], "ɪŋ"))
        candidates.append((word[:-3] + "e", "ɪŋ"))           # writing -> write
    if word.endswith("er") and len(word) > 3:
        candidates.append((word[:-2], "ər"))
        candidates.append((word[:-2] + "e", "ər"))           # taster -> taste
    if word.endswith("est") and len(word) > 4:
        candidates.append((word[:-3], "ɪst"))
        candidates.append((word[:-3] + "e", "ɪst"))
    if word.endswith("ly") and len(word) > 3:
        candidates.append((word[:-2], "li"))
    if word.endswith("ness") and len(word) > 5:
        candidates.append((word[:-4], "nəs"))
    if word.endswith("ful") and len(word) > 4:
        candidates.append((word[:-3], "fʊl"))
    if word.endswith("less") and len(word) > 5:
        candidates.append((word[:-4], "ləs"))
    if word.endswith("ment") and len(word) > 5:
        candidates.append((word[:-4], "mənt"))

    for stem, suffix_code in candidates:
        stem_ipa = resolve_word_ipa(stem, allow_prefix=True, _depth=depth + 1)
        if stem_ipa:
            return _graft_suffix(stem_ipa.strip("/"), suffix_code)
    return None


def _strip_prefix_and_resolve(word, depth):
    """Thử bỏ tiền tố thông dụng (non-, super-, un-...) rồi phiên âm phần còn lại."""
    for prefix in _PREFIXES_BY_LENGTH:
        if word.startswith(prefix) and len(word) - len(prefix) >= 3:
            remainder = word[len(prefix):]
            rest_ipa = resolve_word_ipa(remainder, allow_prefix=False, _depth=depth + 1)
            if rest_ipa:
                return COMMON_PREFIXES_IPA[prefix] + rest_ipa.strip("/")
    return None


def resolve_word_ipa(word, allow_prefix=True, _depth=0):
    """
    Phiên âm 1 từ tiếng Anh qua nhiều tầng:
    từ điển soạn sẵn -> eng_to_ipa -> tách hậu tố -> tách tiền tố
    -> từ điển trực tuyến -> None (để hàm gọi quyết định suy đoán cuối).
    """
    if not word:
        return None
    word = word.lower()

    direct = _direct_lookup(word)
    if direct:
        return direct

    if _depth < 5:  # giới hạn độ sâu đệ quy để tránh lặp vô hạn với từ lạ
        via_suffix = _strip_suffix_and_resolve(word, _depth)
        if via_suffix:
            return via_suffix
        if allow_prefix:
            via_prefix = _strip_prefix_and_resolve(word, _depth)
            if via_prefix:
                return via_prefix

    return _dictionary_api_lookup(word)


def approximate_phonetic_guess(word):
    """
    Phương án CUỐI CÙNG khi không tìm được ở bất kỳ đâu (tên riêng, từ
    viết tắt, từ tự chế...). Đây CHỈ là suy đoán theo quy tắc đánh vần,
    KHÔNG đảm bảo chính xác về ngữ âm — mục đích duy nhất là tránh hiển
    thị y nguyên chữ tiếng Anh như thể đó là phiên âm chuẩn.
    """
    w = word.lower()
    ordered_subs = [
        ("tion", "ʃən"), ("sion", "ʃən"), ("ture", "tʃə"),
        ("ough", "ʌf"), ("augh", "ɑː"), ("eigh", "eɪ"), ("igh", "aɪ"),
        ("ee", "iː"), ("ea", "iː"), ("oo", "uː"), ("ou", "aʊ"),
        ("ow", "aʊ"), ("oy", "ɔɪ"), ("oi", "ɔɪ"), ("ay", "eɪ"),
        ("ai", "eɪ"), ("oa", "əʊ"), ("ph", "f"), ("th", "θ"),
        ("sh", "ʃ"), ("ch", "tʃ"), ("ck", "k"), ("ng", "ŋ"),
        ("qu", "kw"), ("wh", "w"), ("x", "ks"), ("j", "dʒ"),
        ("c", "k"), ("a", "æ"), ("i", "ɪ"), ("o", "ɒ"), ("u", "ʌ"),
    ]
    result = w
    for pattern, repl in ordered_subs:
        result = result.replace(pattern, repl)
    return result


def get_free_ipa_pronunciation(text):
    clean_text = clean_html_for_spellcheck(text)
    if not clean_text:
        return "/.../"

    # Tầng nhanh: thử phiên âm cả đoạn cùng lúc (giữ ngữ điệu/liên kết âm
    # tốt hơn). Chỉ dùng kết quả này nếu KHÔNG có từ nào bị đánh dấu '*'
    # (nghĩa là eng_to_ipa nhận diện được toàn bộ).
    if HAS_ENG_TO_IPA:
        try:
            raw_ipa = ipa.convert(clean_text)
            if raw_ipa and "*" not in raw_ipa:
                return f"/{raw_ipa}/"
        except Exception as e:
            print(f"Lỗi eng_to_ipa: {str(e)}")

    # Tầng phiên âm theo từng từ — áp dụng bộ resolver nhiều tầng cho
    # MỌI từ (không còn nhánh "nếu không có eng_to_ipa mới tra online"
    # như trước, vì từ điển CMU cũng có thể thiếu từ).
    words = re.findall(r"[A-Za-z']+", clean_text.lower())
    results = []
    for raw_word in words:
        word_clean = raw_word.strip("'")
        if not word_clean:
            continue
        ipa_word = resolve_word_ipa(word_clean)
        if not ipa_word:
            ipa_word = approximate_phonetic_guess(word_clean)
        results.append(ipa_word)

    if results:
        return "/" + " ".join(results) + "/"
    return "/ phiên âm tạm thời chưa tải được /"

def free_translate(text, source_lang, target_lang):
    """Dự phòng cuối cùng (Google Translate) — chỉ dùng khi Groq AI thất bại
    hoàn toàn. Đây là bộ dịch máy có xu hướng dịch khá sát từng cụm từ."""
    clean_text = clean_html_for_spellcheck(text)
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={source_lang}&tl={target_lang}&dt=t&q={urllib.parse.quote(clean_text)}"
        response = requests.get(url, timeout=6)
        if response.status_code == 200:
            res_json = response.json()
            translated_text = "".join([part[0] for part in res_json[0] if part[0]])
            return translated_text.strip()
    except Exception as e:
        print(f"Lỗi bộ dịch Google: {str(e)}")
    return ""

# ============================================================
# DỊCH GIỮ NGUYÊN ĐỊNH DẠNG HTML (bold, italic, strike, br...)
# ============================================================

def parse_html_segments(html_text):
    """
    Phân tích HTML thành danh sách các segment dạng:
    {"text": "...", "tags": ["b", "i"], "is_br": False}
    Hỗ trợ: <b>, <strong>, <i>, <em>, <s>, <strike>, <del>, <u>, <br>, <p>, <div>
    Có thể lồng nhau.
    """
    from html.parser import HTMLParser

    segments = []
    current_tags = []

    class SegmentParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tag_lower = tag.lower()
            if tag_lower in ('br',):
                segments.append({"text": "", "tags": list(current_tags), "is_br": True})
            elif tag_lower in ('b', 'strong', 'i', 'em', 's', 'strike', 'del', 'u'):
                current_tags.append(tag_lower)
            elif tag_lower in ('p', 'div'):
                if segments:
                    segments.append({"text": "", "tags": [], "is_br": True})

        def handle_endtag(self, tag):
            tag_lower = tag.lower()
            if tag_lower in ('b', 'strong', 'i', 'em', 's', 'strike', 'del', 'u'):
                # Xóa tag tương ứng (từ cuối để hỗ trợ lồng nhau)
                for i in range(len(current_tags) - 1, -1, -1):
                    canon = {'strong': 'b', 'em': 'i', 'strike': 's', 'del': 's'}.get(current_tags[i], current_tags[i])
                    canon2 = {'strong': 'b', 'em': 'i', 'strike': 's', 'del': 's'}.get(tag_lower, tag_lower)
                    if canon == canon2:
                        current_tags.pop(i)
                        break
            elif tag_lower in ('p', 'div'):
                segments.append({"text": "", "tags": [], "is_br": True})

        def handle_data(self, data):
            if data.strip() or data == ' ':
                segments.append({"text": data, "tags": list(current_tags), "is_br": False})

        def handle_entityref(self, name):
            import html
            segments.append({"text": html.unescape(f'&{name};'), "tags": list(current_tags), "is_br": False})

        def handle_charref(self, name):
            import html
            segments.append({"text": html.unescape(f'&#{name};'), "tags": list(current_tags), "is_br": False})

    parser = SegmentParser()
    parser.feed(html_text)
    return segments

def normalize_tags(tags):
    """Chuẩn hóa tên tag về dạng chuẩn."""
    canon_map = {'strong': 'b', 'em': 'i', 'strike': 's', 'del': 's'}
    return list(dict.fromkeys([canon_map.get(t, t) for t in tags]))

def segments_to_html(segments):
    """Ghép danh sách segment trở lại HTML."""
    result = ""
    for seg in segments:
        if seg.get("is_br"):
            result += "<br>"
            continue
        text = seg["text"]
        if not text:
            continue
        tags = normalize_tags(seg.get("tags", []))
        TAG_MAP = {'b': ('<b>', '</b>'), 'i': ('<i>', '</i>'), 's': ('<s>', '</s>'), 'u': ('<u>', '</u>')}
        open_tags = "".join(TAG_MAP[t][0] for t in tags if t in TAG_MAP)
        close_tags = "".join(TAG_MAP[t][1] for t in reversed(tags) if t in TAG_MAP)
        result += open_tags + text + close_tags
    return result


def _strip_wrapping_quotes(text):
    """Bỏ dấu ngoặc kép bao ngoài nếu AI trả về kèm theo (thường gặp)."""
    if not text:
        return text
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _call_gemini_chat(system_prompt, user_msg, max_tokens=1500, temperature=0.3):
    """
    Hàm gọi Gemini API (Google) — TẦNG DỊCH ƯU TIÊN SỐ 1.
    Trả về chuỗi đã dịch, hoặc None nếu thất bại/quá tải/bị cắt cụt (để
    hàm gọi tự động chuyển sang tầng dự phòng kế tiếp là Groq).

    GHI CHÚ QUAN TRỌNG (nguyên nhân lỗi "dịch nửa chừng rồi dừng"):
    Model gemini-2.5-flash mặc định BẬT chế độ "thinking" (suy luận ẩn),
    và phần suy luận ẩn này TIÊU TỐN CHUNG ngân sách với maxOutputTokens.
    Với đoạn văn dài (nhiều câu/nhiều dòng), phần suy luận ẩn có thể ăn
    gần hết ngân sách token, khiến câu trả lời thực sự bị cắt cụt giữa
    chừng (finishReason = "MAX_TOKENS") nhưng vẫn có vẻ như "thành công"
    vì vẫn có text trả về (chỉ là dở dang). -> Khắc phục bằng 2 cách:
      1) Tắt thinking (thinkingBudget = 0) vì dịch thuật không cần suy
         luận sâu.
      2) Kiểm tra finishReason: nếu là MAX_TOKENS thì coi là THẤT BẠI
         (trả None) để hệ thống tự động chuyển sang Groq, thay vì lặng
         lẽ trả về bản dịch bị cắt cho người dùng.
    """
    if not GEMINI_API_KEY:
        return None
    try:
        headers = {"Content-Type": "application/json"}
        params = {"key": GEMINI_API_KEY}
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }
        resp = requests.post(GEMINI_API_URL, headers=headers, params=params, json=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                cand = candidates[0]
                finish_reason = cand.get("finishReason", "")
                parts = cand.get("content", {}).get("parts", [])
                translated = "".join(p.get("text", "") for p in parts).strip()

                if finish_reason == "MAX_TOKENS":
                    print("Gemini bị CẮT CỤT do hết ngân sách token (MAX_TOKENS) -> "
                          "loại bỏ kết quả dở dang, chuyển sang Groq AI...")
                    return None

                if translated:
                    return _strip_wrapping_quotes(translated)
            print(f"Gemini API: phản hồi rỗng/bị chặn - {str(data)[:300]}")
        elif resp.status_code in _OVERLOAD_STATUS_CODES:
            print(f"Gemini quá tải/lỗi tạm thời ({resp.status_code}) -> chuyển sang Groq AI...")
        else:
            print(f"Gemini API lỗi {resp.status_code}: {resp.text[:300]}")
    except requests.exceptions.Timeout:
        print("Gemini API timeout -> chuyển sang Groq AI...")
    except Exception as e:
        print(f"Lỗi gọi Gemini API: {str(e)}")
    return None


def _call_groq_chat(system_prompt, user_msg, max_tokens=1500, temperature=0.3):
    """
    Hàm gọi Groq API — TẦNG DỊCH DỰ PHÒNG SỐ 2 (khi Gemini quá tải/lỗi).
    - model: openai/gpt-oss-120b (xem ghi chú ở đầu file vì sao đổi model)
    - temperature thấp (0.3): bản dịch ổn định, ít "sáng tạo lệch nghĩa"
    - reasoning_effort: "low" vì dịch thuật không cần suy luận sâu, giúp
      trả lời nhanh hơn trên model dạng reasoning như gpt-oss.
    Trả về chuỗi đã dịch, hoặc None nếu thất bại (để hàm gọi tự quyết định
    phương án dự phòng cuối cùng là Google Translate).
    """
    if not GROQ_API_KEY:
        return None
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": GROQ_TRANSLATE_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": "low",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ]
        }
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason", "")
            translated = choice["message"]["content"].strip()

            if finish_reason == "length":
                print("Groq bị CẮT CỤT do hết ngân sách token (finish_reason=length) -> "
                      "loại bỏ kết quả dở dang, chuyển sang Google Translate...")
                return None

            return _strip_wrapping_quotes(translated)
        elif resp.status_code in _OVERLOAD_STATUS_CODES:
            print(f"Groq cũng quá tải/lỗi tạm thời ({resp.status_code}) -> chuyển sang Google Translate...")
        else:
            print(f"Groq API lỗi {resp.status_code}: {resp.text[:300]}")
    except requests.exceptions.Timeout:
        print("Groq API timeout -> chuyển sang Google Translate...")
    except Exception as e:
        print(f"Lỗi gọi Groq API: {str(e)}")
    return None


def _call_ai_chat(system_prompt, user_msg, max_tokens=1500, temperature=0.3):
    """
    ĐIỀU PHỐI CHUỖI DỊCH AI: Gemini (1) -> Groq (2).
    Đây là hàm DUY NHẤT mà các hàm dịch thuật khác nên gọi để lấy bản dịch
    từ AI. Nếu hàm này trả None nghĩa là cả 2 AI đều thất bại, hàm gọi cần
    tự rơi về Google Translate (free_translate / _translate_plain đã có
    sẵn logic này).
    """
    translated = _call_gemini_chat(system_prompt, user_msg, max_tokens=max_tokens, temperature=temperature)
    if translated:
        return translated

    translated = _call_groq_chat(system_prompt, user_msg, max_tokens=max_tokens, temperature=temperature)
    if translated:
        return translated

    return None


def _build_context_block(context, lang_label_src, lang_label_tgt):
    """
    Dựng đoạn văn bản mô tả NGỮ CẢNH HỘI THOẠI (các câu học gần nhất) để
    chèn vào prompt gửi AI. Giúp AI dịch đúng các câu ngắn/mơ hồ như
    "yes, they do", "me too", "I think so" ... vốn CHỈ có nghĩa đúng khi
    biết câu hỏi/câu trước đó là gì (đây chính là nguyên nhân các lỗi dịch
    sai trong ảnh ví dụ: "yes, they do" bị dịch thành "tôi đồng ý" thay vì
    "Vâng, đúng vậy" vì AI dịch câu đó một cách hoàn toàn cô lập).
    """
    if not context:
        return ""
    lines = []
    for item in context:
        en = (item.get("en") or "").strip()
        vi = (item.get("vi") or "").strip()
        if en:
            lines.append(f"EN: {en}")
        if vi:
            lines.append(f"VI: {vi}")
    if not lines:
        return ""
    return (
        "\n\n[NGỮ CẢNH HỘI THOẠI - các câu học ngay trước câu cần dịch, "
        "CHỈ dùng để hiểu đúng ý nghĩa, KHÔNG dịch lại các câu này]:\n"
        + "\n".join(lines[-8:])
    )


def _split_into_sentences(text, max_chars=350):
    """
    Chia một đoạn text DÀI thành các câu nhỏ hơn (dựa vào dấu . ! ? và
    xuống dòng), mỗi phần không vượt quá ~max_chars ký tự. Mục đích: khi
    gửi cho AI dịch, mỗi lần gọi chỉ chứa 1 lượng nội dung vừa phải, để
    câu trả lời không bao giờ bị dừng giữa chừng do hết ngân sách token,
    và để chuỗi Gemini -> Groq -> Google Translate có thể dịch TOÀN BỘ
    nội dung thay vì chỉ dịch được một phần rồi bỏ dở.
    Nếu đoạn đã đủ ngắn thì trả về nguyên vẹn (không chia).
    """
    if not text or len(text) <= max_chars:
        return [text] if text else []

    # Tách theo dấu kết câu, giữ lại dấu câu ở cuối mỗi phần
    raw_parts = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks = []
    current = ""
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)

    # Trường hợp câu quá dài không có dấu kết câu -> cắt cứng theo độ dài
    # để tuyệt đối không bao giờ tạo ra 1 khối vượt ngưỡng an toàn.
    final_chunks = []
    for c in chunks:
        if len(c) <= max_chars * 1.5:
            final_chunks.append(c)
        else:
            for i in range(0, len(c), max_chars):
                final_chunks.append(c[i:i + max_chars])

    return final_chunks if final_chunks else [text]


def _split_lines_keep_empty(text):
    """Tách text thành các dòng theo ký tự xuống dòng thật (\\n), giữ lại
    cả các dòng trống để có thể tái tạo đúng bố cục khi ghép lại."""
    if text is None:
        return []
    return text.split('\n')


def _update_running_context(running_context, source_lang, original_piece, translated_piece, max_items=6):
    """Thêm 1 cặp (gốc, đã dịch) vào ngữ cảnh đang chạy trong quá trình
    dịch nhiều dòng/nhiều câu của CÙNG một lần gửi, để các phần dịch sau
    hiểu được mạch nội dung của các phần dịch trước (thay vì mỗi dòng bị
    dịch hoàn toàn tách biệt, có thể dẫn đến sai ngữ cảnh)."""
    if not original_piece or not original_piece.strip():
        return running_context
    if source_lang == "en":
        running_context.append({"en": original_piece, "vi": translated_piece or ""})
    else:
        running_context.append({"en": translated_piece or "", "vi": original_piece})
    return running_context[-max_items:]


def _translate_plain_single_chunk(text, source_lang, target_lang, context=None):
    """
    Dịch MỘT khối text thuần (không HTML), đã được đảm bảo đủ ngắn để
    không bị cắt cụt — chuỗi 3 tầng: Gemini AI -> Groq AI -> Google
    Translate. Đây là hàm "nguyên tử", KHÔNG tự chia nhỏ thêm.
    `context`: danh sách các cặp {en, vi} của các câu học/câu liền trước
    gần nhất, dùng để AI hiểu đúng ngữ cảnh (ví dụ câu trả lời ngắn "yes,
    they do" chỉ dịch đúng khi biết câu hỏi trước đó là gì).
    """
    if not text or not text.strip():
        return text

    if source_lang == "en":
        system_prompt = (
            "Bạn là chuyên gia dịch thuật Anh-Việt chuyên nghiệp cho một ứng "
            "dụng học tiếng Anh giao tiếp. "
            "Hãy dịch đoạn sau sang tiếng Việt một cách tự nhiên, mượt mà, đúng "
            "ngữ cảnh, đúng sắc thái — như người Việt bản ngữ viết ra, "
            "KHÔNG dịch máy móc từng từ/từng cụm rời rạc. "
            "Nếu có ngữ cảnh hội thoại đi kèm, PHẢI dựa vào đó để dịch đúng ý "
            "nghĩa thật sự (đặc biệt với các câu trả lời ngắn như 'yes, they "
            "do', 'me too', 'I think so' — không được dịch chung chung, phải "
            "khớp với câu hỏi/ngữ cảnh trước đó). "
            "Chỉ trả về bản dịch ĐẦY ĐỦ, TRỌN VẸN của TOÀN BỘ đoạn văn được "
            "đưa ra — không được bỏ sót, không được dừng lại giữa chừng, "
            "không giải thích, không thêm gì khác."
        )
        user_msg = f'Dịch sang tiếng Việt (dịch trọn vẹn, không bỏ sót phần nào): "{text}"' + _build_context_block(context, "en", "vi")
    else:
        system_prompt = (
            "You are a professional Vietnamese-English translator for an "
            "English-learning conversation app. "
            "Translate the following passage into natural, fluent, idiomatic "
            "English that reads the way a native speaker would write it — "
            "NEVER a literal word-by-word translation. "
            "If conversation context is provided, use it to translate short or "
            "ambiguous replies correctly. "
            "Return ONLY the COMPLETE translation of the ENTIRE passage given — "
            "never skip or stop partway through — no explanation, nothing else."
        )
        user_msg = f'Translate to English (translate it fully, do not skip or stop early): "{text}"' + _build_context_block(context, "vi", "en")

    # max_tokens tính theo độ dài văn bản gốc, có biên độ dư dả để bản dịch
    # (kể cả tiếng Việt vốn dài hơn tiếng Anh) không bao giờ bị hụt token.
    estimated_tokens = max(500, int(len(text.split()) * 4) + 200)
    translated = _call_ai_chat(system_prompt, user_msg, max_tokens=estimated_tokens)
    if translated:
        return translated

    # Fallback cuối cùng: Google Translate
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={source_lang}&tl={target_lang}&dt=t&q={urllib.parse.quote(text)}"
        response = requests.get(url, timeout=6)
        if response.status_code == 200:
            res_json = response.json()
            return "".join([part[0] for part in res_json[0] if part[0]]).strip()
    except Exception as e:
        print(f"Lỗi Google fallback: {e}")

    return text


def _translate_plain(text, source_lang, target_lang, groq_key=None, context=None):
    """
    Dịch một đoạn text thuần (không HTML) — TỰ ĐỘNG CHIA NHỎ nếu đoạn quá
    dài (nhiều câu), dịch TỪNG PHẦN qua chuỗi Gemini -> Groq -> Google
    Translate, rồi ghép lại — đảm bảo dịch TOÀN BỘ nội dung được gửi lên,
    không bao giờ dừng lại giữa chừng như trước đây (khi cả đoạn dài được
    gửi trong 1 lần gọi AI duy nhất và bị cắt cụt do hết ngân sách token).
    `groq_key` giữ lại cho tương thích chữ ký hàm cũ, không còn dùng trực
    tiếp (GROQ_API_KEY đã là biến cấu hình chung ở đầu file).
    `context`: ngữ cảnh hội thoại/câu liền trước (xem _translate_plain_single_chunk).
    """
    if not text or not text.strip():
        return text

    sentence_chunks = _split_into_sentences(text, max_chars=350)

    # Đoạn đủ ngắn -> dịch trực tiếp 1 lần như trước, không cần chia nhỏ
    if len(sentence_chunks) <= 1:
        return _translate_plain_single_chunk(text, source_lang, target_lang, context=context)

    # Đoạn dài -> chia nhỏ theo câu, dịch TUẦN TỰ từng câu, mỗi câu được
    # tham khảo ngữ cảnh của các câu ĐÃ dịch ngay trước đó trong CÙNG đoạn
    # này (để bản dịch mạch lạc, không rời rạc) + ngữ cảnh hội thoại gốc.
    running_context = list(context) if context else []
    translated_pieces = []
    for piece in sentence_chunks:
        piece_translated = _translate_plain_single_chunk(piece, source_lang, target_lang, context=running_context)
        translated_pieces.append(piece_translated)
        running_context = _update_running_context(running_context, source_lang, piece, piece_translated)

    return " ".join(p for p in translated_pieces if p)


# ------------------------------------------------------------
# Dịch HTML có định dạng — TOÀN BỘ trong MỘT lệnh gọi AI duy nhất
# ------------------------------------------------------------
# TRƯỚC ĐÂY: văn bản bị cắt thành từng đoạn nhỏ theo từng cụm bold/
# italic/v.v. rồi dịch riêng từng đoạn -> AI không thấy được toàn câu,
# dẫn đến bản dịch rời rạc, thiếu mạch, gần như "word by word".
#
# BÂY GIỜ: toàn bộ câu được chuyển thành 1 chuỗi text với các thẻ số tạm
# (ví dụ <1>...</1> cho phần bôi đậm, <2/> cho ngắt dòng) rồi gửi NGUYÊN
# CẢ CÂU cho AI dịch một lần — giữ trọn ngữ cảnh để bản dịch tự nhiên,
# mượt mà. Sau khi dịch xong, các thẻ số được ánh xạ ngược lại thành
# <b>/<i>/<s>/<u>/<br> để khôi phục định dạng ban đầu.
#
# Có bước kiểm tra an toàn: nếu AI làm hỏng/làm mất quá nhiều thẻ số,
# hệ thống tự động rơi về phương án dự phòng (dịch từng đoạn như cách cũ)
# để không bao giờ làm hỏng định dạng của người dùng.

_FORMAT_TAG_LETTERS = {'b', 'i', 's', 'u'}
_TAG_TO_HTML = {'b': ('<b>', '</b>'), 'i': ('<i>', '</i>'), 's': ('<s>', '</s>'), 'u': ('<u>', '</u>')}


def _merge_segments(segments):
    """Gom các text-segment liền nhau có cùng tập tags thành 1 nhóm."""
    chunks = []
    for seg in segments:
        if seg["is_br"]:
            chunks.append({"type": "br"})
        else:
            chunks.append({"type": "text", "text": seg["text"], "tags": normalize_tags(seg.get("tags", []))})

    merged = []
    for chunk in chunks:
        if chunk["type"] == "br":
            merged.append(chunk)
        elif merged and merged[-1]["type"] == "text" and merged[-1]["tags"] == chunk["tags"]:
            merged[-1]["text"] += chunk["text"]
        else:
            merged.append(dict(chunk))
    return merged


def _verify_markers_intact(translated_text, tag_map, tolerance=0.34):
    """Kiểm tra xem AI có giữ đủ các thẻ số sau khi dịch không. Nếu mất
    quá nhiều thẻ (>~1/3), coi như không an toàn để ghép lại HTML."""
    if not tag_map:
        return True
    missing = 0
    for n, kind in tag_map.items():
        if kind == "br":
            if f"<{n}/>" not in translated_text:
                missing += 1
        else:
            if f"<{n}>" not in translated_text or f"</{n}>" not in translated_text:
                missing += 1
    return (missing / len(tag_map)) <= tolerance


def _build_marked_text_from_chunks(chunks):
    """
    Giống _build_marked_text_for_translation nhưng chỉ hoạt động trên MỘT
    nhóm chunk KHÔNG chứa ngắt dòng (tức 1 "đoạn/dòng" đã được tách sẵn
    bởi _split_segments_into_paragraphs). Dùng khi chia nhỏ văn bản HTML
    theo từng dòng để dịch, tránh gửi nguyên khối lớn cho AI.
    """
    tag_map = {}
    counter = [0]

    def next_id():
        counter[0] += 1
        return str(counter[0])

    parts = []
    for chunk in chunks:
        tags = [t for t in chunk["tags"] if t in _FORMAT_TAG_LETTERS]
        text = chunk["text"]
        if not tags:
            parts.append(text)
            continue

        ids_for_chunk = []
        open_tags = ""
        for t in tags:
            n = next_id()
            tag_map[n] = t
            ids_for_chunk.append(n)
            open_tags += f"<{n}>"
        close_tags = "".join(f"</{n}>" for n in reversed(ids_for_chunk))
        parts.append(open_tags + text + close_tags)

    return "".join(parts), tag_map


def _split_merged_into_paragraphs(merged):
    """
    Tách danh sách merged-chunk (đã gộp text liền kề cùng định dạng)
    thành các "đoạn/dòng" ngăn cách bởi điểm ngắt dòng (br), để mỗi đoạn
    được dịch RIÊNG trong một lần gọi AI — tránh gửi nguyên khối lớn
    (nhiều dòng/nhiều câu hỏi) trong 1 lần gọi, vốn là nguyên nhân khiến
    bản dịch bị CẮT CỤT giữa chừng khi nội dung dài (xem ảnh ví dụ: chỉ
    dịch được 1 câu trong số nhiều câu rồi dừng).
    Trả về danh sách các phần tử theo đúng thứ tự gốc:
      {"type": "br"}                — một điểm ngắt dòng, giữ nguyên
      {"type": "para", "chunks": [...]} — một đoạn/dòng cần dịch
    """
    paragraphs = []
    current = []
    for chunk in merged:
        if chunk["type"] == "br":
            if current:
                paragraphs.append({"type": "para", "chunks": current})
                current = []
            paragraphs.append({"type": "br"})
        else:
            current.append(chunk)
    if current:
        paragraphs.append({"type": "para", "chunks": current})
    return paragraphs


def _translate_marked_text(marked_text, source_lang, target_lang, context=None):
    """Dịch đoạn text có chứa thẻ số đánh dấu định dạng — TOÀN BỘ trong
    một lệnh gọi AI duy nhất để giữ ngữ cảnh nguyên câu."""
    if not marked_text or not marked_text.strip():
        return marked_text

    tag_rule = (
        "Đoạn văn dưới đây có chứa các thẻ đánh dấu định dạng dạng số, ví dụ "
        "<1>...</1> (đánh dấu phần chữ đậm/nghiêng/gạch ngang/gạch chân) hoặc "
        "<2/> (đánh dấu ngắt dòng). QUY TẮC BẮT BUỘC khi dịch:\n"
        "1. Giữ nguyên y hệt tất cả các thẻ số này trong bản dịch — không đổi "
        "số, không thêm thẻ mới, không xoá thẻ, không dịch hay sửa đổi các "
        "ký tự bên trong dấu < >.\n"
        "2. Được phép di chuyển một thẻ sang vị trí từ/cụm từ tương ứng "
        "trong bản dịch nếu trật tự từ tự nhiên của ngôn ngữ đích khác với "
        "câu gốc — miễn là cặp mở/đóng cùng số vẫn bao đúng phần nội dung "
        "mà nó nhấn mạnh trong câu gốc.\n"
        "3. Dịch toàn bộ đoạn như MỘT câu/đoạn văn liền mạch, tự nhiên, "
        "đúng ngữ cảnh và sắc thái — TUYỆT ĐỐI không dịch máy móc rời rạc "
        "từng từ hay từng cụm nhỏ."
    )

    if source_lang == "en":
        system_prompt = (
            "Bạn là chuyên gia dịch thuật Anh-Việt chuyên nghiệp, dịch tự "
            "nhiên như người bản ngữ viết. " + tag_rule +
            " Chỉ trả về bản dịch (kèm đầy đủ thẻ số), không giải thích gì thêm."
        )
        user_msg = f'Dịch đoạn sau sang tiếng Việt, giữ nguyên các thẻ số:\n"{marked_text}"' + _build_context_block(context, "en", "vi")
    else:
        system_prompt = (
            "You are a professional Vietnamese-English translator who writes "
            "natural, idiomatic English. " + tag_rule.replace(
                "Đoạn văn dưới đây có chứa các thẻ đánh dấu định dạng dạng số, ví dụ "
                "<1>...</1> (đánh dấu phần chữ đậm/nghiêng/gạch ngang/gạch chân) hoặc "
                "<2/> (đánh dấu ngắt dòng). QUY TẮC BẮT BUỘC khi dịch:\n"
                "1. Giữ nguyên y hệt tất cả các thẻ số này trong bản dịch — không đổi "
                "số, không thêm thẻ mới, không xoá thẻ, không dịch hay sửa đổi các "
                "ký tự bên trong dấu < >.\n"
                "2. Được phép di chuyển một thẻ sang vị trí từ/cụm từ tương ứng "
                "trong bản dịch nếu trật tự từ tự nhiên của ngôn ngữ đích khác với "
                "câu gốc — miễn là cặp mở/đóng cùng số vẫn bao đúng phần nội dung "
                "mà nó nhấn mạnh trong câu gốc.\n"
                "3. Dịch toàn bộ đoạn như MỘT câu/đoạn văn liền mạch, tự nhiên, "
                "đúng ngữ cảnh và sắc thái — TUYỆT ĐỐI không dịch máy móc rời rạc "
                "từng từ hay từng cụm nhỏ.",
                "The passage below contains numbered placeholder tags, e.g. "
                "<1>...</1> (marks bold/italic/strikethrough/underline text) or "
                "<2/> (marks a line break). MANDATORY RULES when translating:\n"
                "1. Keep every numbered tag exactly as-is in your translation — "
                "do not renumber, add, remove, translate, or alter the characters "
                "inside the < > marks.\n"
                "2. You MAY move a tag to wherever its corresponding word/phrase "
                "ends up in the translation if natural target-language word order "
                "differs from the source — as long as the matching open/close pair "
                "still wraps the same emphasized content as in the source.\n"
                "3. Translate the whole passage as ONE coherent, natural, "
                "context-appropriate text — NEVER a disjointed word-by-word or "
                "phrase-by-phrase translation."
            ) +
            " Return ONLY the translation (with all numbered tags intact), nothing else."
        )
        user_msg = f'Translate the following into English, keeping the numbered tags:\n"{marked_text}"' + _build_context_block(context, "vi", "en")

    estimated_tokens = max(700, int(len(marked_text.split()) * 4) + 300)
    return _call_ai_chat(system_prompt, user_msg, max_tokens=estimated_tokens)


def _apply_marked_translation_to_html(translated_marked_text, tag_map):
    """Parse bản dịch (vẫn chứa các thẻ số) thành HTML thật với
    <b>/<i>/<s>/<u>/<br>. Cố gắng khôi phục tối đa, bỏ qua an toàn nếu
    gặp thẻ lạ/không khớp thay vì làm vỡ output."""
    token_re = re.compile(r'<(\d+)(/)?>|</(\d+)>')
    result = []
    open_stack = []  # [(tag_number, letter), ...]
    pos = 0

    for m in token_re.finditer(translated_marked_text):
        result.append(translated_marked_text[pos:m.start()])
        pos = m.end()

        if m.group(1) and m.group(2):          # thẻ tự đóng <N/>
            kind = tag_map.get(m.group(1))
            if kind == "br":
                result.append("<br>")
        elif m.group(1):                        # thẻ mở <N>
            n = m.group(1)
            letter = tag_map.get(n)
            if letter in _TAG_TO_HTML:
                open_stack.append((n, letter))
                result.append(_TAG_TO_HTML[letter][0])
        elif m.group(3):                        # thẻ đóng </N>
            n = m.group(3)
            for i in range(len(open_stack) - 1, -1, -1):
                if open_stack[i][0] == n:
                    result.append(_TAG_TO_HTML[open_stack[i][1]][1])
                    open_stack.pop(i)
                    break

    result.append(translated_marked_text[pos:])

    # Đóng nốt các thẻ mà AI quên đóng, để không vỡ định dạng trang
    for n, letter in reversed(open_stack):
        result.append(_TAG_TO_HTML[letter][1])

    return "".join(result)


def _translate_preserving_html_fallback(segments, source_lang, target_lang, context=None):
    """
    Phương án DỰ PHÒNG (chỉ chạy khi cách dịch 1-lần-toàn-câu ở trên thất
    bại hoặc AI làm hỏng thẻ số quá nhiều): dịch riêng từng đoạn định
    dạng như cách cũ. Kém mượt hơn nhưng đảm bảo không bao giờ vỡ layout.
    """
    merged = _merge_segments(segments)

    translated_merged = []
    for chunk in merged:
        if chunk["type"] == "br":
            translated_merged.append(chunk)
        else:
            translated_text = _translate_plain(chunk["text"], source_lang, target_lang, context=context)
            translated_merged.append({"type": "text", "text": translated_text, "tags": chunk["tags"]})

    result_html = ""
    for chunk in translated_merged:
        if chunk["type"] == "br":
            result_html += "<br>"
        else:
            tags = chunk["tags"]
            text = chunk["text"]
            open_t = "".join(_TAG_TO_HTML[t][0] for t in tags if t in _TAG_TO_HTML)
            close_t = "".join(_TAG_TO_HTML[t][1] for t in reversed(tags) if t in _TAG_TO_HTML)
            result_html += open_t + text + close_t

    return result_html


def translate_preserving_html(html_text, source_lang, target_lang, context=None):
    """
    Dịch nội dung HTML giữ nguyên định dạng (bold, italic, strike, br...).

    CHIẾN LƯỢC (đã cải tiến để KHÔNG BAO GIỜ dịch dở dang):
    Toàn bộ nội dung được TÁCH THEO TỪNG DÒNG/ĐOẠN (ngăn cách bởi <br>,
    tương ứng với cách người dùng xuống dòng khi nhập nhiều câu/nhiều ý
    trong 1 lần "DỊCH & LƯU"). MỖI DÒNG được dịch RIÊNG qua chuỗi
    Gemini AI -> Groq AI -> Google Translate, rồi mới ghép lại thành HTML
    hoàn chỉnh. Nhờ vậy:
      - Không có lệnh gọi AI nào phải "gánh" quá nhiều nội dung cùng lúc
        -> không còn bị cắt cụt giữa chừng do hết ngân sách token (lỗi
        trong ảnh ví dụ: gửi 4 câu hỏi nhưng chỉ dịch được 1 câu rồi
        dừng).
      - Từng dòng vẫn được dịch tự nhiên, đúng ngữ cảnh nhờ được truyền
        kèm NGỮ CẢNH của các dòng NGAY TRƯỚC ĐÓ trong cùng đoạn (và ngữ
        cảnh hội thoại trước đó nếu có), nên không bị "rời rạc từng câu".
      - Nếu 1 dòng vẫn còn dài (nhiều câu) thì bản thân _translate_plain
        sẽ tiếp tục tự chia nhỏ theo câu.
    """
    segments = parse_html_segments(html_text)
    if not segments:
        return free_translate(html_text, source_lang, target_lang)

    merged = _merge_segments(segments)
    paragraphs = _split_merged_into_paragraphs(merged)

    running_context = list(context) if context else []
    result_parts = []

    for item in paragraphs:
        if item["type"] == "br":
            result_parts.append("<br>")
            continue

        chunks = item["chunks"]
        plain_preview = "".join(c["text"] for c in chunks)
        if not plain_preview.strip():
            continue

        marked_text, tag_map = _build_marked_text_from_chunks(chunks)

        if not tag_map:
            # Dòng này không có định dạng đậm/nghiêng/gạch... -> dịch
            # thẳng (hàm _translate_plain tự chia nhỏ thêm nếu quá dài)
            translated_line_html = _translate_plain(marked_text, source_lang, target_lang, context=running_context)
        else:
            translated_marked = _translate_marked_text(marked_text, source_lang, target_lang, context=running_context)
            if translated_marked and _verify_markers_intact(translated_marked, tag_map):
                translated_line_html = _apply_marked_translation_to_html(translated_marked, tag_map)
            else:
                # AI làm hỏng thẻ số của riêng dòng này -> dịch dự phòng
                # an toàn CHỈ cho dòng này (không ảnh hưởng các dòng khác)
                fallback_segments = [{"text": c["text"], "tags": c["tags"], "is_br": False} for c in chunks]
                translated_line_html = _translate_preserving_html_fallback(fallback_segments, source_lang, target_lang, context=running_context)

        result_parts.append(translated_line_html)

        translated_plain = clean_html_for_spellcheck(translated_line_html)
        running_context = _update_running_context(running_context, source_lang, plain_preview, translated_plain)

    return "".join(result_parts)

def _translate_sentence_atomic(clean_text, source_lang, target_lang, context=None):
    """Dịch MỘT câu/khối text thuần đã đủ ngắn (không tự chia nhỏ thêm).
    Đây là phần logic dịch câu gốc (kèm ví dụ xử lý ngữ cảnh câu trả lời
    ngắn như 'yes, they do')."""
    if source_lang == "en" and target_lang == "vi":
        system_prompt = (
            "Bạn là chuyên gia dịch thuật Anh-Việt cho một ứng dụng học tiếng "
            "Anh giao tiếp (nhật ký học các câu hội thoại). "
            "Hãy dịch câu sau sang tiếng Việt một cách tự nhiên, đúng ngữ cảnh, "
            "đúng văn phong hội thoại đời thường — không dịch từng từ máy móc "
            "(word-by-word). Giữ đúng ý nghĩa, sắc thái và phong cách của câu "
            "gốc. Nếu có ngữ cảnh hội thoại (các câu trước đó) đi kèm, PHẢI "
            "dựa vào đó để chọn nghĩa đúng — đặc biệt với câu trả lời ngắn "
            "kiểu 'yes, they do' / 'yes, I do' / 'me too' / 'I think so', "
            "hãy dịch theo đúng ý xác nhận/trả lời câu hỏi trước đó (ví dụ "
            "'yes, they do' nên dịch là 'Vâng, đúng vậy' hoặc 'Có, họ có', "
            "TUYỆT ĐỐI không dịch thành 'tôi đồng ý'). "
            "Chỉ trả về bản dịch ĐẦY ĐỦ của TOÀN BỘ câu — không bỏ sót, "
            "không dừng lại giữa chừng, không giải thích, không thêm gì khác."
        )
        user_msg = f'Dịch sang tiếng Việt (dịch trọn vẹn): "{clean_text}"' + _build_context_block(context, "en", "vi")
    else:
        system_prompt = (
            "Bạn là chuyên gia dịch thuật Việt-Anh cho một ứng dụng học tiếng "
            "Anh giao tiếp (nhật ký học các câu hội thoại). "
            "Hãy dịch câu sau sang tiếng Anh một cách tự nhiên, đúng ngữ cảnh, "
            "đúng văn phong hội thoại đời thường — không dịch từng từ máy móc "
            "(word-by-word). Giữ đúng ý nghĩa, sắc thái và phong cách của câu "
            "gốc. Nếu có ngữ cảnh hội thoại (các câu trước đó) đi kèm, PHẢI "
            "dựa vào đó để dịch đúng ý, đúng thì, đúng đại từ. "
            "Chỉ trả về bản dịch ĐẦY ĐỦ của TOÀN BỘ câu — không bỏ sót, "
            "không dừng lại giữa chừng, không giải thích, không thêm gì khác."
        )
        user_msg = f'Translate to English (translate it fully): "{clean_text}"' + _build_context_block(context, "vi", "en")

    estimated_tokens = max(500, int(len(clean_text.split()) * 4) + 200)
    translated = _call_ai_chat(system_prompt, user_msg, max_tokens=estimated_tokens)
    if translated:
        return translated

    print("Cả Gemini và Groq đều thất bại -> chuyển sang Google Translate (fallback)...")
    return free_translate(clean_text, source_lang, target_lang)


def _translate_sentence(text, source_lang, target_lang, context=None):
    """Dịch một câu/1 dòng — TỰ ĐỘNG chia nhỏ theo câu nếu quá dài, để
    không bao giờ bị cắt cụt giữa chừng (giống cơ chế của _translate_plain)."""
    if not text or not text.strip():
        return text

    sentence_chunks = _split_into_sentences(text, max_chars=350)
    if len(sentence_chunks) <= 1:
        return _translate_sentence_atomic(text, source_lang, target_lang, context=context)

    running_context = list(context) if context else []
    translated_pieces = []
    for piece in sentence_chunks:
        piece_translated = _translate_sentence_atomic(piece, source_lang, target_lang, context=running_context)
        translated_pieces.append(piece_translated)
        running_context = _update_running_context(running_context, source_lang, piece, piece_translated)
    return " ".join(p for p in translated_pieces if p)


def smart_translate(text, source_lang, target_lang, context=None):
    """Dịch thông minh — tự động giữ định dạng HTML nếu có, luôn dịch theo
    ngữ cảnh nguyên câu (và ngữ cảnh hội thoại trước đó nếu có) thay vì
    rời rạc từng từ. Chuỗi dịch: Gemini AI -> Groq AI -> Google Translate.
    Nội dung nhiều dòng/nhiều câu được TỰ ĐỘNG CHIA NHỎ (theo dòng, rồi
    theo câu nếu vẫn còn dài) để dịch TOÀN BỘ, không bao giờ dừng lại
    giữa chừng."""
    # Kiểm tra text có chứa HTML tags định dạng không
    has_formatting = bool(re.search(r'<(b|strong|i|em|s|strike|del|u|br)[^>]*>', text, re.IGNORECASE))

    if has_formatting:
        return translate_preserving_html(text, source_lang, target_lang, context=context)

    # Không có HTML — dịch thông thường, tự chia theo dòng nếu có nhiều dòng
    clean_text = clean_html_for_spellcheck(text)
    if not clean_text:
        return ""

    lines = clean_text.split('\n')
    real_lines = [ln for ln in lines if ln.strip()]

    # Chỉ có 1 dòng (trường hợp phổ biến nhất: 1 câu) -> dịch như cũ
    if len(real_lines) <= 1:
        return _translate_sentence(clean_text, source_lang, target_lang, context=context)

    # Nhiều dòng -> dịch TUẦN TỰ từng dòng, tích lũy ngữ cảnh giữa các
    # dòng để bản dịch mạch lạc, rồi ghép lại đúng bố cục xuống dòng gốc.
    running_context = list(context) if context else []
    translated_lines = []
    for line in lines:
        if not line.strip():
            translated_lines.append("")
            continue
        line_translated = _translate_sentence(line, source_lang, target_lang, context=running_context)
        translated_lines.append(line_translated)
        running_context = _update_running_context(running_context, source_lang, line, line_translated)

    return "\n".join(translated_lines)

def smart_context_analyzer(text):
    text_lower = text.lower()
    local_feedback = []
    if "lean" in text_lower:
        if any(keyword in text_lower for keyword in ["english", "vietnamese", "language", "skill", "math", "vocabulary", "lesson"]):
            local_feedback.append("💡 **Gợi ý ngữ cảnh:** Bạn đang dùng từ 'lean' (nghiêng). Có phải bạn muốn viết là **'learn'** (học tập) không?")
    if "loose" in text_lower:
        if any(phrase in text_lower for phrase in ["want to", "going to", "will"]) and any(word in text_lower for word in ["weight", "money", "game"]):
            local_feedback.append("💡 **Gợi ý ngữ cảnh:** Bạn đang dùng từ 'loose' (lỏng lẻo). Khi nói về việc đánh mất, bạn cần dùng từ **'lose'**.")
    if "advice" in text_lower:
        if re.search(r'\b(i|you|we|they|he|she|will|should|must|to)\s+advice\b', text_lower):
            local_feedback.append("💡 **Gợi ý ngữ cảnh:** Bạn dùng 'advice' (danh từ) ở vị trí của động từ. Hãy sửa thành động từ **'advise'**.")
    return local_feedback

def check_grammar_with_languagetool(text, is_eng):
    if not is_eng or not text:
        return "✅ Ngữ pháp & Chính tả chuẩn."

    clean_text = clean_html_for_spellcheck(text)
    errors_list = []

    context_suggestions = smart_context_analyzer(clean_text)
    if context_suggestions:
        errors_list.extend(context_suggestions)

    if is_eng and clean_text and clean_text[0].islower():
        errors_list.append("⚠️ Lỗi ngữ pháp: Câu tiếng Anh bắt đầu bằng chữ cái thường. Bạn cần viết hoa chữ cái đầu tiên.")

    try:
        url = "https://api.languagetool.org/v2/check"
        response = requests.post(url, data={"text": clean_text, "language": "en-US"}, timeout=6)
        if response.status_code == 200:
            result = response.json()
            matches = result.get("matches", [])
            for match in matches[:3]:
                message = match.get("message", "")
                vi_message = free_translate(message, "en", "vi")
                offset = match.get("offset", 0)
                length = match.get("length", 0)
                wrong_word = clean_text[offset:offset+length]
                replacements = [r.get("value") for r in match.get("replacements", [])[:2]]
                rep_text = f" -> Nên sửa thành: " + ", ".join([f"'{r}'" for r in replacements]) if replacements else ""
                if any(wrong_word in sugg for sugg in context_suggestions):
                    continue
                errors_list.append(f"❌ Lỗi {len(errors_list)+1}: Từ '{wrong_word}'. {vi_message}{rep_text}")
    except Exception as e:
        print(f"Lỗi LanguageTool: {str(e)}")

    if errors_list:
        return "\n".join(errors_list)

    fallback_errors = []
    if clean_text and clean_text[-1] not in ['.', '!', '?']:
        fallback_errors.append("⚠️ Cuối câu thiếu dấu kết thúc.")
    return "\n".join(fallback_errors) if fallback_errors else "✅ Ngữ pháp & Chính tả chuẩn."


# ============================================================
# ROUTES - GIAO DIỆN
# ============================================================

@app.route('/')
def index():
    return render_template('index.html')


# ============================================================
# ROUTES - XÁC THỰC
# ============================================================

@app.route('/api/verify_password', methods=['POST'])
def api_verify_password():
    """
    Xác thực đăng nhập.
    - Admin: sys_username kết thúc _admin + đúng SYSTEM_PASSWORD
    - User con: sys_username là tên user con + đúng mật khẩu riêng của user đó
    Trả về: { status, role: "admin"|"user", username, display_name }
    """
    req_data = request.get_json()
    sys_username = req_data.get('sys_username', '').strip().lower()
    password     = req_data.get('password', '').strip()

    if not sys_username or not password:
        return jsonify({"status": "error", "message": "Vui lòng nhập đầy đủ thông tin!"}), 400

    # --- Kiểm tra admin ---
    if is_admin_user(sys_username):
        if password == SYSTEM_PASSWORD:
            return jsonify({
                "status": "success",
                "role": "admin",
                "username": sys_username,
                "display_name": sys_username.replace('_admin', '').upper()
            })
        else:
            return jsonify({"status": "error", "message": "Mật khẩu Admin không chính xác!"}), 401

    # --- Kiểm tra user con ---
    users_db = load_users_db()
    if sys_username in users_db:
        user_info = users_db[sys_username]
        if user_info.get('password') == password:
            return jsonify({
                "status": "success",
                "role": "user",
                "username": sys_username,
                "display_name": sys_username.upper()
            })
        else:
            return jsonify({"status": "error", "message": "Mật khẩu không chính xác!"}), 401

    return jsonify({"status": "error", "message": "Tài khoản không tồn tại!"}), 401


# ============================================================
# ROUTES - QUẢN LÝ USER (CHỈ ADMIN)
# ============================================================

@app.route('/api/admin/list_users', methods=['GET'])
def api_admin_list_users():
    """Admin lấy danh sách tất cả user con."""
    if not verify_admin_password(request.args):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    users_db = load_users_db()
    user_list = []
    for uname, info in users_db.items():
        log_count = len(load_data(uname))
        user_list.append({
            "username": uname,
            "display_name": info.get("display_name", uname.upper()),
            "created_at": info.get("created_at", ""),
            "log_count": log_count
        })
    return jsonify({"status": "success", "users": user_list})


@app.route('/api/admin/create_user', methods=['POST'])
def api_admin_create_user():
    """Admin tạo user con mới."""
    req_data = request.get_json()
    if not verify_admin_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    new_username = req_data.get('new_username', '').strip().lower()
    new_password = req_data.get('new_password', '').strip()
    display_name = req_data.get('display_name', new_username).strip()

    if not new_username or not new_password:
        return jsonify({"status": "error", "message": "Vui lòng nhập đầy đủ tên và mật khẩu!"}), 400

    # Không cho tạo username kết thúc _admin
    if new_username.endswith('_admin'):
        return jsonify({"status": "error", "message": "Tên người dùng không hợp lệ!"}), 400

    # Chỉ cho phép chữ thường, số, gạch dưới
    if not re.match(r'^[a-z0-9_]+$', new_username):
        return jsonify({"status": "error", "message": "Tên chỉ được dùng chữ thường, số, gạch dưới!"}), 400

    users_db = load_users_db()
    if new_username in users_db:
        return jsonify({"status": "error", "message": f"Người dùng '{new_username}' đã tồn tại!"}), 409

    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    created_at = datetime.now(tz_vn).strftime("%d/%m/%Y %H:%M")

    users_db[new_username] = {
        "password": new_password,
        "display_name": display_name if display_name else new_username.upper(),
        "created_at": created_at
    }
    save_users_db(users_db)

    return jsonify({
        "status": "success",
        "message": f"Tạo tài khoản '{new_username}' thành công!",
        "user": {
            "username": new_username,
            "display_name": users_db[new_username]["display_name"],
            "created_at": created_at,
            "log_count": 0
        }
    })


@app.route('/api/admin/delete_user', methods=['POST'])
def api_admin_delete_user():
    """Admin xóa user con (kèm dữ liệu học)."""
    req_data = request.get_json()
    if not verify_admin_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    target_user = req_data.get('target_username', '').strip().lower()
    if not target_user:
        return jsonify({"status": "error", "message": "Thiếu tên người dùng!"}), 400

    users_db = load_users_db()
    if target_user not in users_db:
        return jsonify({"status": "error", "message": "Người dùng không tồn tại!"}), 404

    del users_db[target_user]
    save_users_db(users_db)

    # Xóa file dữ liệu học (tùy chọn - giữ lại data để an toàn)
    # log_file = os.path.join(DATA_DIR, f'learning_log_{target_user}.json')
    # if os.path.exists(log_file):
    #     os.remove(log_file)

    return jsonify({"status": "success", "message": f"Đã xóa tài khoản '{target_user}'!"})


@app.route('/api/admin/update_user_password', methods=['POST'])
def api_admin_update_user_password():
    """Admin đổi mật khẩu cho user con."""
    req_data = request.get_json()
    if not verify_admin_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    target_user  = req_data.get('target_username', '').strip().lower()
    new_password = req_data.get('new_password', '').strip()

    if not target_user or not new_password:
        return jsonify({"status": "error", "message": "Thiếu thông tin!"}), 400

    users_db = load_users_db()
    if target_user not in users_db:
        return jsonify({"status": "error", "message": "Người dùng không tồn tại!"}), 404

    users_db[target_user]['password'] = new_password
    save_users_db(users_db)
    return jsonify({"status": "success", "message": "Đổi mật khẩu thành công!"})


@app.route('/api/admin/all_logs', methods=['GET'])
def api_admin_all_logs():
    """Admin xem nhật ký học của tất cả user con."""
    if not verify_admin_password(request.args):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    users_db = load_users_db()
    all_data = {}
    for uname in users_db:
        all_data[uname] = {
            "display_name": users_db[uname].get("display_name", uname.upper()),
            "logs": load_data(uname)
        }
    return jsonify({"status": "success", "all_data": all_data})


# ============================================================
# ROUTES - HỌC TẬP (ADMIN + USER CON)
# ============================================================

@app.route('/api/load_log', methods=['GET'])
def api_load_log():
    if not verify_request_password(request.args):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    username = request.args.get('username', '').strip().lower()
    if not username:
        return jsonify({"status": "error", "message": "Thiếu tên người dùng"}), 400
    return jsonify({"status": "success", "data": load_data(username)})


@app.route('/api/translate', methods=['POST'])
def api_translate():
    req_data = request.get_json()
    if not verify_request_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    username = req_data.get('username', '').strip().lower()
    text = req_data.get('text', '').strip()

    if not username or not text:
        return jsonify({"status": "error", "message": "Dữ liệu không hợp lệ"}), 400

    text_clean_check = clean_html_for_spellcheck(text)
    vi_chars = len(re.findall(r'[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]', text_clean_check.lower()))
    is_eng = True if vi_chars <= 1 else False

    sl = 'en' if is_eng else 'vi'
    tl = 'vi' if is_eng else 'en'

    tz_vietnam = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vietnam = datetime.now(tz_vietnam)
    formatted_time = now_vietnam.strftime("%H:%M")
    today_str = now_vietnam.strftime("%d.%m.%Y")

    # Lấy vài câu học GẦN NHẤT làm ngữ cảnh hội thoại, giúp AI dịch đúng
    # ý nghĩa các câu ngắn/mơ hồ (vd: "yes, they do") thay vì dịch cô lập.
    current_log = load_data(username)
    conversation_context = current_log[-4:] if current_log else []

    res = smart_translate(text, sl, tl, context=conversation_context)
    if not res:
        res = "(Hệ thống dịch đang bận, vui lòng thử lại sau)"

    english_text = text if is_eng else res
    pronunciation = get_free_ipa_pronunciation(english_text)
    grammar_result = check_grammar_with_languagetool(text, is_eng)

    display_date = f"{today_str} {formatted_time}"

    if res and not res.startswith("("):
        current_log.append({
            "en": text if is_eng else english_text,
            "vi": res if is_eng else text,
            "ipa": pronunciation,
            "date": display_date,
            "full_date": today_str
        })
        save_data(username, current_log)

    return jsonify({
        "status": "success",
        "result": res,
        "ipa": pronunciation,
        "grammar": grammar_result,
        "updated_data": current_log
    })


@app.route('/api/edit', methods=['POST'])
def api_edit():
    req_data = request.get_json()
    if not verify_request_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    username = req_data.get('username', '').strip().lower()
    idx = req_data.get('index')

    log = load_data(username)
    if 0 <= idx < len(log):
        new_en = req_data.get('en', '').strip()
        new_vi = req_data.get('vi', '').strip()
        new_ipa = get_free_ipa_pronunciation(new_en)

        log[idx]['en'] = new_en
        log[idx]['vi'] = new_vi
        log[idx]['ipa'] = new_ipa

        save_data(username, log)
        return jsonify({
            "status": "success",
            "data": log,
            "updated_ipa": new_ipa
        })
    return jsonify({"status": "error", "message": "Không tìm thấy câu học"}), 404


@app.route('/api/delete', methods=['POST'])
def api_delete():
    req_data = request.get_json()
    if not verify_request_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    username = req_data.get('username', '').strip().lower()
    idx = req_data.get('index')

    log = load_data(username)
    if 0 <= idx < len(log):
        del log[idx]
        save_data(username, log)
        return jsonify({"status": "success", "data": log})
    return jsonify({"status": "error", "message": "Lỗi vị trí xóa"}), 404


@app.route('/api/preview_ipa', methods=['POST'])
def api_preview_ipa():
    req_data = request.get_json()
    if not verify_request_password(req_data):
        return jsonify({"status": "error", "message": "Truy cập trái phép!"}), 401

    text = req_data.get('text', '').strip()
    if not text:
        return jsonify({"status": "error", "message": "Thiếu nội dung"}), 400

    ipa_result = get_free_ipa_pronunciation(text)
    return jsonify({"status": "success", "ipa": ipa_result})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)