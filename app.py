import json
import os
import re
import html as html_lib
import base64
import requests
import time
import subprocess
import tempfile
import threading
import pytz
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import eng_to_ipa as ipa
    HAS_ENG_TO_IPA = True
except ImportError:
    HAS_ENG_TO_IPA = False

app = Flask(__name__)

# ============================================================
# CẤU HÌNH HỆ THỐNG
# ============================================================
SYSTEM_PASSWORD = os.environ.get("SYSTEM_PASSWORD", "th@nh341978")   # Mật khẩu hệ thống chung (admin dùng khi đăng nhập lớp 1)
# Ưu tiên đọc từ biến môi trường SYSTEM_PASSWORD (đặt trong dashboard
# Render / PythonAnywhere) để không lộ mật khẩu admin ngay trong source
# code. Giá trị cũ được giữ làm fallback -> nếu bạn CHƯA khai báo biến
# môi trường này trên server, app vẫn chạy y hệt như trước (không đổi
# hành vi). Khi đã set biến môi trường, fallback này sẽ không được dùng
# tới nữa và có thể xoá đi để bắt buộc dùng biến môi trường.

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


def _github_list_dir(repo_path):
    """
    Liệt kê tên các file trong 1 thư mục của repo GitHub (Contents API).
    Trả về list tên file, hoặc None nếu gọi API thất bại (mất mạng,
    GitHub sập...). Dùng để biết đầy đủ danh sách file 'learning_log_*'
    hiện có trên GitHub mà không cần đoán trước tên user.
    """
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{repo_path}"
    try:
        resp = requests.get(
            url, headers=_github_headers(),
            params={"ref": GITHUB_BRANCH}, timeout=10
        )
        if resp.status_code == 200:
            payload = resp.json()
            return [item["name"] for item in payload if item.get("type") == "file"]
        else:
            print(f"GitHub LIST '{repo_path}' lỗi {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        print(f"Lỗi gọi GitHub LIST '{repo_path}': {str(e)}")
        return None


def _github_get_file(repo_path):
    """
    Đọc 1 file từ repo GitHub qua Contents API.

    Trả về dict với 3 khả năng, PHÂN BIỆT RÕ giữa "file không tồn tại"
    (hợp lệ, ví dụ user mới chưa có log) và "gọi GitHub bị lỗi" (mất
    mạng, timeout, GitHub sập, token sai...) — 2 trường hợp cũ trước đây
    bị gộp chung thành None, khiến khi GitHub sập, chương trình tưởng
    nhầm là "chưa có dữ liệu" và xoá sạch mọi thứ hiển thị cho người
    dùng thay vì dùng bản cache cục bộ:
        {"found": True,  "error": False, "text": "...", "sha": "..."}  -> đọc được
        {"found": False, "error": False, "text": None,  "sha": None }  -> 404, file thật sự chưa có
        {"found": False, "error": True,  "text": None,  "sha": None }  -> lỗi gọi API, KHÔNG phải là "chưa có"
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
            return {"found": True, "error": False, "text": text, "sha": sha}
        elif resp.status_code == 404:
            return {"found": False, "error": False, "text": None, "sha": None}
        else:
            print(f"GitHub GET '{repo_path}' lỗi {resp.status_code}: {resp.text[:200]}")
            return {"found": False, "error": True, "text": None, "sha": None}
    except Exception as e:
        print(f"Lỗi gọi GitHub GET '{repo_path}': {str(e)}")
        return {"found": False, "error": True, "text": None, "sha": None}


def _github_put_file(repo_path, text_content, commit_message,
                      max_retries=3, timeout=30):
    """
    Ghi (tạo mới hoặc cập nhật) 1 file trong repo GitHub qua Contents API.
    Trả về True nếu thành công, False nếu thất bại.

    LỊCH SỬ SỬA LỖI (quan trọng, đọc trước khi sửa lại hàm này):
    - Bản đầu tiên dùng requests.put(..., timeout=10) đơn giản -> với file
      lớn, hay bị "OSError: write error" (timeout/mất kết nối âm thầm).
    - Bản thứ hai thử tự ép cứng header Content-Length -> gây lỗi MỚI:
      GitHub trả "400 - malformed request" ngay cả với file không quá
      lớn.
    - Bản thứ ba quay về requests.put(..., json=payload) (để requests tự
      lo hết) -> VẪN bị lỗi 400 y hệt với file ~750KB (learning_log_thanh
      .json), trong khi curl gửi CHÍNH XÁC cùng nội dung đó qua dòng lệnh
      lại THÀNH CÔNG (200 OK). Điều này CHỨNG MINH bằng thực nghiệm rằng
      lỗi không phải do kích thước hay do proxy hạ tầng, mà do cách thư
      viện `requests`/urllib3 dựng request PUT thân lớn trong môi trường
      PythonAnywiwe free cụ thể này (rất có thể liên quan cách nó xử lý
      kết nối/khung dữ liệu khi đi qua proxy whitelist của họ).
    -> GIẢI PHÁP CUỐI: gọi thẳng lệnh `curl` (đã được xác nhận hoạt động
      ổn định với file ~775KB) thông qua subprocess, thay vì dùng thư
      viện `requests` cho riêng thao tác PUT này. Việc đọc file (GET) vẫn
      dùng `requests` như cũ vì chưa từng gặp lỗi.
    """
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{repo_path}"
    b64_content = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")

    def _attempt(sha):
        """
        Gửi 1 lần PUT qua curl (subprocess). Trả về (status_code:int,
        body_text:str). status_code = 0 nếu curl tự thân thất bại (mất
        mạng, timeout...) trước khi có phản hồi HTTP nào.
        """
        payload = {
            "message": commit_message,
            "content": b64_content,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        payload_file = None
        output_file = None
        try:
            # Ghi payload ra file tạm (an toàn hơn truyền qua dòng lệnh
            # với payload lớn/ký tự đặc biệt) rồi trỏ curl đọc bằng @file.
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as pf:
                json.dump(payload, pf, ensure_ascii=True)
                payload_file = pf.name

            out_fd, output_file = tempfile.mkstemp(suffix=".json")
            os.close(out_fd)

            cmd = [
                "curl", "-s", "-X", "PUT",
                "-H", f"Authorization: Bearer {GITHUB_TOKEN}",
                "-H", "Accept: application/vnd.github+json",
                "-H", "Content-Type: application/json",
                "-H", "X-GitHub-Api-Version: 2022-11-28",
                url,
                "-d", f"@{payload_file}",
                "-o", output_file,
                "-w", "%{http_code}",
                "--max-time", str(timeout),
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout + 15
            )

            status_str = (result.stdout or "").strip()
            status_code = int(status_str) if status_str.isdigit() else 0

            body_text = ""
            if output_file and os.path.exists(output_file):
                with open(output_file, "r", encoding="utf-8", errors="replace") as of:
                    body_text = of.read()

            if status_code == 0:
                # curl không lấy được HTTP status hợp lệ -> coi là lỗi
                # mạng để tầng gọi quyết định thử lại.
                err_detail = (result.stderr or body_text or "không rõ nguyên nhân")[:300]
                raise ConnectionError(f"curl thất bại: {err_detail}")

            return status_code, body_text
        finally:
            for f in (payload_file, output_file):
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

    last_network_error = None
    try:
        cached_sha = _GITHUB_SHA_CACHE.get(repo_path)

        for attempt in range(1, max_retries + 1):
            try:
                status_code, body_text = _attempt(cached_sha)
            except (subprocess.TimeoutExpired, ConnectionError) as e:
                # Lỗi nghi do mạng/proxy -> thử lại.
                last_network_error = e
                print(f"GitHub PUT '{repo_path}' lỗi mạng (lần {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2 ** (attempt - 1))  # backoff: 1s, 2s, 4s...
                    continue
                print(f"GitHub PUT '{repo_path}' thất bại sau {max_retries} lần thử do lỗi mạng.")
                return False

            # sha lưu trong bộ nhớ đệm bị cũ (file đã đổi trên GitHub từ
            # nơi khác) -> đọc lại sha mới nhất rồi thử ghi lại.
            if status_code == 409:
                existing = _github_get_file(repo_path)
                cached_sha = existing["sha"] if existing else None
                try:
                    status_code, body_text = _attempt(cached_sha)
                except (subprocess.TimeoutExpired, ConnectionError) as e:
                    last_network_error = e
                    print(f"GitHub PUT '{repo_path}' lỗi mạng sau khi làm mới sha (lần {attempt}/{max_retries}): {e}")
                    if attempt < max_retries:
                        time.sleep(2 ** (attempt - 1))
                        continue
                    return False

            if status_code in (200, 201):
                try:
                    new_sha = (json.loads(body_text).get("content") or {}).get("sha")
                    if new_sha:
                        _GITHUB_SHA_CACHE[repo_path] = new_sha
                except Exception:
                    pass
                return True

            # 5xx (lỗi tạm thời phía GitHub/proxy) -> đáng để thử lại.
            if 500 <= status_code < 600:
                print(f"GitHub PUT '{repo_path}' lỗi {status_code} (lần {attempt}/{max_retries}): {body_text[:300]}")
                if attempt < max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                return False

            # Lỗi 4xx còn lại (sai token, payload không hợp lệ...) -> lỗi
            # logic, retry thêm cũng vô ích, dừng ngay.
            print(f"GitHub PUT '{repo_path}' lỗi {status_code}: {body_text[:300]}")
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
# ------------------------------------------------------------------
# AN TOÀN API KEY — BẮT BUỘC ĐỌC:
# TUYỆT ĐỐI KHÔNG hard-code key thật vào code (dù trong file này hay bất
# kỳ file nào khác được commit lên GitHub) — repo public thì Google/
# GitHub sẽ tự động quét ra và khoá key ngay (lỗi "reported as leaked").
# Toàn bộ key phải đặt qua biến môi trường trên Render / PythonAnywhere
# (Dashboard -> Environment), KHÔNG đặt giá trị mặc định thật ở đây.
#
# HỖ TRỢ NHIỀU KEY (xoay vòng chống nghẽn 429 / khoá 403):
# Đặt nhiều key cách nhau bằng dấu phẩy, ví dụ trên Render:
#   GEMINI_API_KEYS = AIzaSy-key-1,AIzaSy-key-2,AIzaSy-key-3
#   GROQ_API_KEYS   = gsk_key-1,gsk_key-2
# (Vẫn hỗ trợ biến số ít GEMINI_API_KEY / GROQ_API_KEY cho tương thích
# ngược, chỉ 1 key.)
# ------------------------------------------------------------------
def _load_key_list(*env_names):
    """Đọc danh sách key từ biến môi trường, hỗ trợ 2 kiểu đặt tên:
      1) Gộp 1 biến, nhiều key cách nhau bằng dấu phẩy:
         GEMINI_API_KEYS = key1,key2,key3
      2) Nhiều biến đánh số riêng lẻ (đúng kiểu đang dùng trên Render):
         GEMINI_API_KEY, GEMINI_API_KEY1, GEMINI_API_KEY2, GEMINI_API_KEY3, ...
         GROQ_API_KEY, GROQ_API_KEY2, GROQ_API_KEY3, ... (không cần liền số,
         không có KEY1 vẫn không sao, sẽ tự bỏ qua và đọc tiếp KEY2, KEY3...)
    Gộp toàn bộ key tìm được từ cả 2 kiểu lại, loại trùng, giữ thứ tự.
    """
    keys = []
    seen = set()

    def _add(raw):
        for k in raw.split(","):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                keys.append(k)

    # Kiểu 1: biến số nhiều, gộp bằng dấu phẩy (vd GEMINI_API_KEYS)
    for name in env_names:
        raw = os.environ.get(name, "")
        if raw.strip():
            _add(raw)

    # Kiểu 2: biến đánh số riêng lẻ, dựa trên tên số ít trong env_names
    # (vd GEMINI_API_KEY, GEMINI_API_KEY1 .. GEMINI_API_KEY20)
    singular_names = [n for n in env_names if not n.endswith("S")]
    for base in singular_names:
        # base không hậu tố số (GEMINI_API_KEY) đã đọc ở vòng trên nếu có
        # nhưng đọc lại ở đây phòng trường hợp chỉ set kiểu số ít
        raw = os.environ.get(base, "")
        if raw.strip():
            _add(raw)
        for i in range(1, 21):  # hỗ trợ tới 20 key đánh số, dư sức dùng
            raw = os.environ.get(f"{base}{i}", "")
            if raw.strip():
                _add(raw)

    return keys

GEMINI_API_KEYS = _load_key_list("GEMINI_API_KEYS", "GEMINI_API_KEY")
# CẬP NHẬT 20/07/2026: Google đã khai tử "gemini-2.5-flash" (model cũ trả về
# lỗi 404 "no longer available to new users" với key mới, và bị giới hạn
# quota gắt với key cũ). Đổi sang "gemini-3.5-flash" — model GA (ổn định,
# sẵn sàng cho production) hiện được Google khuyến nghị thay thế.
GEMINI_TRANSLATE_MODEL = "gemini-3.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TRANSLATE_MODEL}:generateContent"

GROQ_API_KEYS = _load_key_list("GROQ_API_KEYS", "GROQ_API_KEY")
GROQ_TRANSLATE_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

if not GEMINI_API_KEYS:
    print("CẢNH BÁO: chưa cấu hình GEMINI_API_KEYS/GEMINI_API_KEY -> tầng dịch Gemini sẽ bị bỏ qua.")
if not GROQ_API_KEYS:
    print("CẢNH BÁO: chưa cấu hình GROQ_API_KEYS/GROQ_API_KEY -> tầng dịch Groq sẽ bị bỏ qua.")

# Mã lỗi HTTP coi là "quá tải / hết hạn mức" -> thử key kế tiếp (nếu còn),
# hết key thì mới chuyển sang tầng dịch kế tiếp
_OVERLOAD_STATUS_CODES = {429, 500, 502, 503, 504}
# Mã lỗi coi là "key hỏng/bị khoá vĩnh viễn" -> loại key này khỏi vòng
# xoay ngay lập tức (khác với quá tải tạm thời), thử key kế tiếp
_DEAD_KEY_STATUS_CODES = {401, 403}

# NGÂN SÁCH THỜI GIAN TỔNG (chống lỗi "WORKER TIMEOUT" trên Render/gunicorn):
# Gunicorn mặc định giết worker nếu 1 request xử lý quá X giây (đã tăng lên
# 60s qua --timeout 60, xem hướng dẫn Start Command). Một đoạn văn dài bị
# TỰ ĐỘNG CHIA thành nhiều "chunk" nhỏ (theo dòng, rồi theo câu) và dịch
# TUẦN TỰ từng chunk — nếu mỗi chunk tự cấp lại ngân sách 22s riêng, tổng
# thời gian cả request cộng dồn RẤT NHANH vượt quá giới hạn của gunicorn.
# -> Giải pháp: dùng 1 "deadline" DÙNG CHUNG cho TOÀN BỘ request (mọi
# chunk), lưu trong biến thread-local (mỗi request 1 giá trị riêng, an
# toàn khi có nhiều worker/thread). Route /api/translate gọi
# `_start_request_ai_deadline()` ngay khi bắt đầu xử lý; mọi lệnh gọi
# Gemini/Groq bên trong request đó tự động dùng chung deadline này. Hễ hết
# ngân sách, các chunk còn lại BỎ QUA thẳng Gemini/Groq, dùng Google
# Translate (luôn nhanh, vài giây) để đảm bảo tổng thời gian không bao giờ
# vượt quá giới hạn gunicorn.
TIMEOUT_PER_CALL = 4            # giây, timeout cho MỖI lần gọi Gemini/Groq
                                 # (siết chặt xuống 3.5-5s thay vì 8s cũ —
                                 # mục tiêu: 1 lần gọi bị treo/nghẽn không
                                 # bao giờ được phép "ngốn" quá nhiều ngân
                                 # sách thời gian chung của request, để tổng
                                 # độ trễ cộng dồn không bao giờ chạm ngưỡng
                                 # WORKER TIMEOUT của Gunicorn/uWSGI).
AI_TOTAL_TIME_BUDGET = 35       # giây, tổng thời gian tối đa cho TOÀN BỘ
                                 # request (mọi chunk cộng lại), chừa dư
                                 # >20s cho các lệnh gọi Google Translate
                                 # dự phòng + xử lý khác trước khi chạm
                                 # ngưỡng 60s của gunicorn.

_ai_deadline_local = threading.local()


def _start_request_ai_deadline():
    """Gọi 1 LẦN DUY NHẤT ở đầu mỗi route xử lý dịch thuật (vd api_translate)
    để mở 1 ngân sách thời gian MỚI, dùng chung cho toàn bộ request đó
    (bất kể request được chia thành bao nhiêu chunk nhỏ bên trong)."""
    _ai_deadline_local.value = time.time() + AI_TOTAL_TIME_BUDGET
    _start_request_circuit_breaker()


def _get_request_ai_deadline():
    """Lấy deadline dùng chung của request hiện tại. Nếu chưa có route nào
    khởi tạo (vd gọi hàm dịch ngoài ngữ cảnh HTTP request, hoặc quên gọi
    _start_request_ai_deadline), tự tạo 1 ngân sách MỚI để hàm gọi vẫn
    hoạt động an toàn (không None -> không giới hạn thời gian)."""
    deadline = getattr(_ai_deadline_local, "value", None)
    if deadline is None:
        deadline = time.time() + AI_TOTAL_TIME_BUDGET
        _ai_deadline_local.value = deadline
    return deadline


# ------------------------------------------------------------
# CIRCUIT BREAKER (ngắt mạch theo từng request)
# ------------------------------------------------------------
# VẤN ĐỀ: trước đây, dù 1 provider (Gemini/Groq) đang bị nghẽn/rớt mạng,
# hệ thống vẫn tiếp tục "thử lại" provider đó ở MỌI chunk tiếp theo của
# cùng 1 request -> mỗi lần thử lại lại tốn thêm vài giây timeout, cộng
# dồn qua hàng chục chunk là nguyên nhân chính gây WORKER TIMEOUT.
#
# GIẢI PHÁP: mỗi request có 1 bộ đếm lỗi LIÊN TIẾP riêng (thread-local,
# giống _ai_deadline_local) cho từng provider. Chỉ tính là "lỗi" theo đúng
# yêu cầu: Timeout hoặc HTTP 429. Hễ 1 provider bị lỗi liên tiếp đủ
# CIRCUIT_BREAKER_THRESHOLD lần, mạch của provider đó bị "ngắt" (tripped)
# cho đến hết request hiện tại -> MỌI chunk còn lại tự động BỎ QUA hẳn
# provider đó (không thử lại nữa, không tốn thêm 1 giây timeout nào),
# rơi thẳng xuống tầng dự phòng kế tiếp (Groq, rồi Google Translate).
CIRCUIT_BREAKER_THRESHOLD = 2   # số lỗi Timeout/429 LIÊN TIẾP để ngắt mạch

_circuit_breaker_local = threading.local()


def _start_request_circuit_breaker():
    """Mở lại bộ đếm circuit breaker SẠCH cho 1 request mới (gọi cùng lúc
    với _start_request_ai_deadline)."""
    _circuit_breaker_local.state = {
        "gemini": {"consecutive_fails": 0, "tripped": False},
        "groq":   {"consecutive_fails": 0, "tripped": False},
    }


def _get_circuit_breaker_state():
    state = getattr(_circuit_breaker_local, "state", None)
    if state is None:
        state = {
            "gemini": {"consecutive_fails": 0, "tripped": False},
            "groq":   {"consecutive_fails": 0, "tripped": False},
        }
        _circuit_breaker_local.state = state
    return state


def _cb_is_tripped(provider):
    """True nếu provider ('gemini'/'groq') đã bị ngắt mạch cho request
    hiện tại -> hàm gọi PHẢI bỏ qua hẳn provider này, không thử nữa."""
    return _get_circuit_breaker_state()[provider]["tripped"]


def _cb_record_failure(provider):
    """Ghi nhận 1 lỗi Timeout/429 của provider. Trả về True nếu lần ghi
    nhận này vừa làm mạch bị NGẮT (để hàm gọi dừng thử các key còn lại
    ngay lập tức thay vì tốn thêm thời gian)."""
    st = _get_circuit_breaker_state()[provider]
    st["consecutive_fails"] += 1
    just_tripped = False
    if st["consecutive_fails"] >= CIRCUIT_BREAKER_THRESHOLD and not st["tripped"]:
        st["tripped"] = True
        just_tripped = True
        print(f"[CircuitBreaker] {provider}: {st['consecutive_fails']} lỗi "
              f"Timeout/429 LIÊN TIẾP -> NGẮT MẠCH provider này cho TOÀN BỘ "
              f"các chunk còn lại của request hiện tại.")
    return just_tripped


def _cb_record_success(provider):
    """Gọi thành công -> reset bộ đếm lỗi liên tiếp về 0. (Không tự động
    'đóng lại' mạch đã bị ngắt trong CÙNG request — 1 lần ngắt là ngắt cho
    hết request đó, tránh dao động lãng phí thời gian; mạch sẽ tự mở lại
    sạch sẽ ở request KẾ TIẾP)."""
    st = _get_circuit_breaker_state()[provider]
    st["consecutive_fails"] = 0

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
    """Ghi JSON xuống đĩa cục bộ — dùng làm bản cache dự phòng song song với GitHub."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# ------------------------------------------------------------
# HÀNG ĐỢI ĐỒNG BỘ LẠI GITHUB (khi GitHub bị lỗi/mất mạng)
# ------------------------------------------------------------
# Mọi file dữ liệu (danh sách user, nhật ký học...) đều được ghi xuống
# đĩa cục bộ TRƯỚC (xem _local_save_json ở trên), sau đó mới ghi lên
# GitHub. Nếu bước ghi GitHub thất bại, file đó được ghi tên vào
# '_pending_sync.json' -> lần request kế tiếp gọi tới bất kỳ hàm
# load/save dữ liệu nào cũng sẽ thử đồng bộ lại các file đang chờ này
# lên GitHub, để khi GitHub hoạt động trở lại thì 2 nguồn (GitHub +
# cache cục bộ trên PythonAnywhere) tự khớp lại với nhau mà không cần
# admin phải làm gì thủ công.
PENDING_SYNC_FILE = os.path.join(DATA_DIR, '_pending_sync.json')
_last_pending_flush_attempt = 0
_PENDING_FLUSH_COOLDOWN_SECONDS = 60   # tránh gọi GitHub liên tục mỗi request khi đang sập kéo dài


def _mark_pending_sync(repo_path, local_filepath, commit_message):
    """Ghi nhận 1 file chưa đồng bộ lên GitHub được, để thử lại sau."""
    pending = _local_load_json(PENDING_SYNC_FILE, {})
    pending[repo_path] = {"local_file": local_filepath, "commit_message": commit_message}
    _local_save_json(PENDING_SYNC_FILE, pending)


def _clear_pending_sync(repo_path):
    """Bỏ đánh dấu 1 file khỏi hàng đợi sau khi đã đồng bộ GitHub thành công."""
    pending = _local_load_json(PENDING_SYNC_FILE, {})
    if repo_path in pending:
        del pending[repo_path]
        _local_save_json(PENDING_SYNC_FILE, pending)


def _flush_pending_sync():
    """
    Thử đồng bộ lại tất cả file đang chờ (do lần trước ghi GitHub thất
    bại) lên GitHub. Được gọi ở đầu mỗi hàm load/save dữ liệu -> tự
    "chữa lành" ngay khi GitHub hoạt động trở lại, không cần chờ admin
    thao tác thủ công. Có cooldown để không làm chậm mọi request nếu
    GitHub đang sập kéo dài.
    """
    global _last_pending_flush_attempt
    if not _github_storage_configured():
        return
    now = time.time()
    if now - _last_pending_flush_attempt < _PENDING_FLUSH_COOLDOWN_SECONDS:
        return
    _last_pending_flush_attempt = now

    pending = _local_load_json(PENDING_SYNC_FILE, {})
    if not pending:
        return
    print(f"[ĐỒNG BỘ] Có {len(pending)} file đang chờ đồng bộ lên GitHub -> đang thử lại...")
    for repo_path, info in list(pending.items()):
        local_file = info.get("local_file")
        commit_message = info.get("commit_message", f"Đồng bộ lại {repo_path} sau khi GitHub khôi phục")
        data = _local_load_json(local_file, None)
        if data is None:
            _clear_pending_sync(repo_path)
            continue
        text = json.dumps(data, ensure_ascii=False, indent=4)
        ok = _github_put_file(repo_path, text, commit_message, max_retries=1, timeout=15)
        if ok:
            _clear_pending_sync(repo_path)
            print(f"[ĐỒNG BỘ] Đã đồng bộ lại thành công lên GitHub: {repo_path}")
        else:
            print(f"[ĐỒNG BỘ] Vẫn chưa đồng bộ được: {repo_path} (sẽ thử lại ở request sau)")


def _hash_password(plain_password):
    """Băm mật khẩu bằng werkzeug (thuật toán mặc định: pbkdf2:sha256 + salt)."""
    return generate_password_hash(plain_password)


def _is_hashed(stored_value):
    """Nhận diện chuỗi đã là hash werkzeug hay còn là mật khẩu plaintext cũ."""
    return isinstance(stored_value, str) and (
        stored_value.startswith('pbkdf2:') or
        stored_value.startswith('scrypt:') or
        stored_value.startswith('argon2:')
    )


def _verify_user_password(stored_value, provided_password):
    """
    So khớp mật khẩu, hỗ trợ cả 2 dạng lưu trữ:
    - Đã hash (dữ liệu mới): dùng check_password_hash.
    - Còn plaintext (dữ liệu cũ từ trước khi có bản vá này): so sánh trực
      tiếp để tài khoản cũ vẫn đăng nhập được bình thường, không bị gãy.
    """
    if _is_hashed(stored_value):
        return check_password_hash(stored_value, provided_password)
    return stored_value == provided_password


def load_users_db():
    """
    Tải danh sách user con.
    Ưu tiên đọc từ file 'userdata/_users.json' trong repo GitHub (bền
    vững trên mọi server). Nếu GitHub bị lỗi (mất mạng, GitHub sập, token
    sai...) thì đọc từ bản cache cục bộ (file cùng nội dung được lưu mỗi
    lần ghi/đọc thành công gần nhất) để chương trình vẫn dùng được thay
    vì hiện ra trống trơn. Nếu file thật sự chưa tồn tại (404, hợp lệ)
    thì trả về rỗng như bình thường.

    LƯU Ý QUAN TRỌNG: mỗi lần đọc THÀNH CÔNG từ GitHub, dữ liệu cũng được
    ghi đè xuống cache cục bộ CỦA SERVER ĐANG XỬ LÝ REQUEST NÀY. Lý do:
    PythonAnywhere và Render là 2 server độc lập, ổ đĩa cục bộ không dùng
    chung -> nếu thay đổi chỉ đến từ server A (ví dụ Render), cache cục
    bộ của server B (PythonAnywhere) sẽ không tự biết mà cập nhật trừ khi
    có ai đó đọc/ghi dữ liệu ngay trên chính server B. Ghi đè cache ngay
    lúc đọc đảm bảo hễ server nào có người mở app lên là cache của nó tự
    làm mới theo đúng bản mới nhất trên GitHub.
    """
    _flush_pending_sync()
    if _github_storage_configured():
        result = _github_get_file(f"{GITHUB_DATA_PATH}/_users.json")
        if result["error"]:
            print("[CẢNH BÁO] Không đọc được _users.json từ GitHub (lỗi kết nối/API) -> dùng bản cache cục bộ.")
            return _local_load_json(USERS_FILE, {})
        if not result["found"]:
            return {}
        try:
            users_db = json.loads(result["text"]) if result["text"].strip() else {}
            _local_save_json(USERS_FILE, users_db)   # đồng bộ cache cục bộ theo GitHub
            return users_db
        except Exception:
            print("Lỗi đọc _users.json từ GitHub (JSON hỏng) -> dùng bản cache cục bộ.")
            return _local_load_json(USERS_FILE, {})
    return _local_load_json(USERS_FILE, {})


def save_users_db(users_db):
    """
    Lưu danh sách user con.
    Luôn ghi 1 bản cache xuống đĩa cục bộ TRƯỚC (để luôn có bản mới nhất
    sẵn sàng dùng nếu GitHub sập lúc đọc sau này), rồi mới ghi lên GitHub
    (nguồn chính, bền vững qua các lần deploy/restart). Nếu ghi GitHub
    thất bại, bản cache cục bộ vừa ghi vẫn đảm bảo không mất thao tác của
    người dùng ngay tại thời điểm đó, và file được đưa vào hàng đợi để
    tự đồng bộ lại lên GitHub ngay khi GitHub hoạt động trở lại.
    """
    _flush_pending_sync()
    _local_save_json(USERS_FILE, users_db)
    repo_path = f"{GITHUB_DATA_PATH}/_users.json"
    if _github_storage_configured():
        text = json.dumps(users_db, ensure_ascii=False, indent=4)
        ok = _github_put_file(repo_path, text, "Cập nhật danh sách tài khoản người học")
        if ok:
            _clear_pending_sync(repo_path)
        else:
            print("[CẢNH BÁO] Ghi _users.json lên GitHub thất bại -> đã có bản cache cục bộ dự phòng, đưa vào hàng đợi đồng bộ lại.")
            _mark_pending_sync(repo_path, USERS_FILE, "Cập nhật danh sách tài khoản người học")


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

def _extract_password(req_data_or_args):
    """
    Lấy mật khẩu từ request — CHỈ đọc từ header 'Authorization: Bearer <password>'.

    LÝ DO: nếu password nằm trong query string (?password=...) hoặc URL,
    nó sẽ bị ghi lại nguyên văn vào access log của hạ tầng (Render/uWSGI/
    PythonAnywhere...), vào header Referer của các request kế tiếp, và vào
    lịch sử trình duyệt. Header Authorization thì KHÔNG bị các server access
    log tiêu chuẩn ghi lại theo cách đó, nên đây là chỗ đúng để gửi password.

    KHÔNG còn fallback đọc 'password' từ query string/body nữa — toàn bộ
    frontend (index.html) đã chuyển sang gửi header Authorization. Nếu vẫn
    giữ fallback, ai đó (hoặc chính bạn quên) gọi API kiểu cũ
    '?password=...' vẫn sẽ được chấp nhận và tiếp tục bị ghi vào access log
    y như trước — tức là chưa vá được lỗ hổng thật sự.
    """
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        return auth_header[7:].strip()
    return ''


def verify_request_password(req_data_or_args):
    """
    Xác thực request cho các route học tập:
    - Chấp nhận SYSTEM_PASSWORD (admin)
    - Chấp nhận mật khẩu riêng của user con (kèm theo username)
    """
    provided_password = _extract_password(req_data_or_args)
    if provided_password == SYSTEM_PASSWORD:
        return True
    # Kiểm tra mật khẩu user con
    username = req_data_or_args.get('username', '').strip().lower()
    if username:
        users_db = load_users_db()
        if username in users_db:
            return _verify_user_password(users_db[username].get('password', ''), provided_password)
    return False

def verify_admin_password(req_data_or_args):
    """Chỉ chấp nhận SYSTEM_PASSWORD — dùng cho các route quản trị."""
    provided_password = _extract_password(req_data_or_args)
    return provided_password == SYSTEM_PASSWORD

def load_data(username):
    """
    Tải nhật ký học của 1 người dùng.
    Ưu tiên đọc từ 'userdata/learning_log_<username>.json' trong repo
    GitHub. Nếu GitHub bị lỗi (mất mạng, GitHub sập, token sai...) thì
    đọc từ bản cache cục bộ thay vì trả về rỗng — để người học vẫn xem
    và tiếp tục học được ngay cả khi GitHub đang gặp sự cố. Nếu file
    thật sự chưa tồn tại (404, ví dụ user mới chưa học bài nào) thì trả
    về rỗng như bình thường.

    Mỗi lần đọc THÀNH CÔNG từ GitHub cũng ghi đè xuống cache cục bộ của
    server đang xử lý request này (xem giải thích chi tiết trong
    load_users_db) — để cache của PythonAnywhere/Render tự cập nhật theo
    GitHub dù thay đổi được lưu từ server kia.
    """
    _flush_pending_sync()
    uname = username.lower()
    filename = os.path.join(DATA_DIR, f'learning_log_{uname}.json')
    if _github_storage_configured():
        result = _github_get_file(f"{GITHUB_DATA_PATH}/learning_log_{uname}.json")
        if result["error"]:
            print(f"[CẢNH BÁO] Không đọc được learning_log_{uname}.json từ GitHub (lỗi kết nối/API) -> dùng bản cache cục bộ.")
            return _local_load_json(filename, [])
        if not result["found"]:
            return []
        try:
            data = json.loads(result["text"]) if result["text"].strip() else []
            _local_save_json(filename, data)   # đồng bộ cache cục bộ theo GitHub
            return data
        except Exception:
            print(f"Lỗi đọc learning_log_{uname}.json từ GitHub (JSON hỏng) -> dùng bản cache cục bộ.")
            return _local_load_json(filename, [])
    return _local_load_json(filename, [])


def save_data(username, data):
    """
    Lưu nhật ký học của 1 người dùng.
    Luôn ghi 1 bản cache xuống đĩa cục bộ TRƯỚC (đảm bảo luôn có bản mới
    nhất để dùng nếu GitHub sập lúc đọc sau này), rồi mới ghi lên GitHub
    — đây là bước chạy MỖI KHI người học thêm/sửa/xoá 1 câu. Nếu ghi
    GitHub thất bại, bản cache cục bộ vừa ghi vẫn đảm bảo không mất thao
    tác của người dùng ngay tại thời điểm đó, và file được đưa vào hàng
    đợi để tự đồng bộ lại lên GitHub ngay khi GitHub hoạt động trở lại.
    """
    _flush_pending_sync()
    uname = username.lower()
    filename = os.path.join(DATA_DIR, f'learning_log_{uname}.json')
    _local_save_json(filename, data)
    repo_path = f"{GITHUB_DATA_PATH}/learning_log_{uname}.json"
    if _github_storage_configured():
        text = json.dumps(data, ensure_ascii=False, indent=4)
        ok = _github_put_file(repo_path, text, f"Cập nhật nhật ký học của '{uname}'")
        if ok:
            _clear_pending_sync(repo_path)
        else:
            print(f"[CẢNH BÁO] Ghi learning_log_{uname}.json lên GitHub thất bại -> đã có bản cache cục bộ dự phòng, đưa vào hàng đợi đồng bộ lại.")
            _mark_pending_sync(repo_path, filename, f"Cập nhật nhật ký học của '{uname}'")

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


# ------------------------------------------------------------
# PARALLEL GOOGLE TRANSLATE FALLBACK (concurrent.futures)
# ------------------------------------------------------------
# VẤN ĐỀ CŨ: khi Gemini/Groq hết ngân sách thời gian, các chunk còn lại
# rơi xuống Google Translate nhưng vẫn được gọi TUẦN TỰ từng chunk một
# (for chunk in chunks: free_translate(chunk)) -> dù mỗi lần gọi chỉ ~1-2s,
# với văn bản bị chia thành hàng chục chunk thì độ trễ vẫn CỘNG DỒN đáng
# kể, góp phần gây WORKER TIMEOUT.
# GIẢI PHÁP: dùng ThreadPoolExecutor để bắn TẤT CẢ các chunk cần fallback
# sang Google Translate CÙNG LÚC (song song), tổng thời gian chờ chỉ còn
# xấp xỉ thời gian của chunk CHẬM NHẤT thay vì tổng tất cả các chunk.
GOOGLE_TRANSLATE_MAX_WORKERS = 8   # nằm trong khoảng khuyến nghị 5-10


def _parallel_google_translate(texts, source_lang, target_lang):
    """Dịch SONG SONG nhiều đoạn text độc lập bằng Google Translate (tầng
    dự phòng cuối cùng), thay vì dịch tuần tự từng đoạn.
    Trả về list bản dịch cùng thứ tự, cùng độ dài với `texts` (không bao
    giờ trả None — nếu 1 đoạn dịch lỗi thì giữ nguyên văn bản gốc của
    đoạn đó, để không làm mất nội dung của người dùng)."""
    if not texts:
        return []
    if len(texts) == 1:
        translated = free_translate(texts[0], source_lang, target_lang)
        return [translated or texts[0]]

    results = [None] * len(texts)
    max_workers = max(1, min(GOOGLE_TRANSLATE_MAX_WORKERS, len(texts)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(free_translate, t, source_lang, target_lang): i
            for i, t in enumerate(texts)
        }
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                results[i] = future.result()
            except Exception as e:
                print(f"[Parallel Google Translate] Lỗi ở chunk #{i}: {str(e)}")
                results[i] = ""

    return [r if r else texts[i] for i, r in enumerate(results)]


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


def _call_gemini_chat(system_prompt, user_msg, max_tokens=1500, temperature=0.3, deadline=None):
    """
    Hàm gọi Gemini API (Google) — TẦNG DỊCH ƯU TIÊN SỐ 1.
    Trả về chuỗi đã dịch, hoặc None nếu thất bại/quá tải/bị cắt cụt/hết
    ngân sách thời gian (để hàm gọi tự động chuyển sang tầng dự phòng kế
    tiếp là Groq).

    XOAY VÒNG NHIỀU KEY: nếu có nhiều key trong GEMINI_API_KEYS, lần lượt
    thử từng key — key nào bị quá tải (429/5xx) hoặc bị khoá (401/403)
    thì bỏ qua ngay, thử key kế tiếp; chỉ khi TẤT CẢ key đều thất bại
    (hoặc hết ngân sách thời gian `deadline`) mới trả None để rơi sang
    Groq.

    `deadline`: mốc thời gian (time.time() + số giây) mà TOÀN BỘ chuỗi
    Gemini+Groq phải xong trước đó — chống lỗi gunicorn "WORKER TIMEOUT"
    khi thử quá nhiều key/tầng liên tiếp (xem ghi chú AI_TOTAL_TIME_BUDGET
    ở đầu file).

    GHI CHÚ QUAN TRỌNG (nguyên nhân lỗi "dịch nửa chừng rồi dừng"):
    Các model Gemini 3.x mặc định BẬT chế độ "thinking" (suy luận ẩn), và
    phần suy luận ẩn này TIÊU TỐN CHUNG ngân sách với maxOutputTokens.
    Với đoạn văn dài (nhiều câu/nhiều dòng), phần suy luận ẩn có thể ăn
    gần hết ngân sách token, khiến câu trả lời thực sự bị cắt cụt giữa
    chừng (finishReason = "MAX_TOKENS") nhưng vẫn có vẻ như "thành công"
    vì vẫn có text trả về (chỉ là dở dang). -> Khắc phục bằng 2 cách:
      1) Đặt thinkingLevel = "minimal" (tham số MỚI thay cho thinkingBudget
         đã lỗi thời từ Gemini 3.x — dịch thuật không cần suy luận sâu).
      2) Kiểm tra finishReason: nếu là MAX_TOKENS thì coi là THẤT BẠI
         (trả None) để hệ thống tự động chuyển sang Groq, thay vì lặng
         lẽ trả về bản dịch bị cắt cho người dùng.
    """
    if not GEMINI_API_KEYS:
        return None

    # CIRCUIT BREAKER: nếu Gemini đã bị ngắt mạch (2 lỗi Timeout/429 liên
    # tiếp) ở 1 chunk TRƯỚC ĐÓ trong CÙNG request này -> bỏ qua thẳng,
    # không tốn thêm 1 giây timeout nào nữa, rơi thẳng xuống Groq.
    if _cb_is_tripped("gemini"):
        print("Gemini: mạch đã bị NGẮT do lỗi liên tiếp trước đó trong request này -> bỏ qua, chuyển thẳng sang Groq AI...")
        return None

    headers = {"Content-Type": "application/json"}
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        }
    }

    for idx, api_key in enumerate(GEMINI_API_KEYS):
        key_label = f"key #{idx + 1}/{len(GEMINI_API_KEYS)}"

        if deadline is not None and time.time() >= deadline:
            print(f"Gemini: hết ngân sách thời gian trước khi thử {key_label} -> chuyển sang Groq AI...")
            return None

        remaining = TIMEOUT_PER_CALL
        if deadline is not None:
            remaining = max(1, min(TIMEOUT_PER_CALL, deadline - time.time()))

        try:
            params = {"key": api_key}
            resp = requests.post(GEMINI_API_URL, headers=headers, params=params, json=payload, timeout=remaining)
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
                        _cb_record_success("gemini")
                        return _strip_wrapping_quotes(translated)
                print(f"Gemini API ({key_label}): phản hồi rỗng/bị chặn - {str(data)[:300]}")
            elif resp.status_code == 429:
                # 429 = quá tải hạn mức -> tính vào circuit breaker (đúng
                # yêu cầu: Timeout / HTTP 429 là 2 loại lỗi "đếm" ngắt mạch)
                if _cb_record_failure("gemini"):
                    print("Gemini: đã ngắt mạch sau lỗi 429 liên tiếp -> dừng thử các key còn lại, chuyển sang Groq AI...")
                    return None
                print(f"Gemini ({key_label}) lỗi 429 (quá tải hạn mức) -> thử key kế tiếp...")
                continue
            elif resp.status_code in _DEAD_KEY_STATUS_CODES:
                print(f"Gemini API ({key_label}) lỗi {resp.status_code} (key hỏng/bị khoá) -> thử key kế tiếp...")
                continue
            elif resp.status_code in _OVERLOAD_STATUS_CODES:
                print(f"Gemini ({key_label}) quá tải/lỗi tạm thời ({resp.status_code}) -> thử key kế tiếp...")
                continue
            else:
                print(f"Gemini API ({key_label}) lỗi {resp.status_code}: {resp.text[:300]}")
        except requests.exceptions.Timeout:
            # Timeout THẬT (server không phản hồi kịp) thường là do sự cố
            # kết nối/mạng tới CHÍNH dịch vụ đó (không riêng gì 1 key) ->
            # thử thêm key khác của CÙNG Gemini thường cũng sẽ timeout y
            # hệt, chỉ tổ tốn thêm hàng chục giây. Dừng NGAY toàn bộ vòng
            # xoay key Gemini, chuyển thẳng sang Groq (server khác hẳn).
            # Đồng thời tính vào circuit breaker: 2 timeout liên tiếp (kể
            # cả ở các chunk KHÁC nhau của cùng request) sẽ ngắt mạch hẳn.
            print(f"Gemini API ({key_label}) timeout -> bỏ qua các key Gemini còn lại, chuyển thẳng sang Groq AI...")
            _cb_record_failure("gemini")
            break
        except Exception as e:
            print(f"Lỗi gọi Gemini API ({key_label}): {str(e)}")
            continue

    print("Tất cả Gemini API key đều thất bại -> chuyển sang Groq AI...")
    return None


def _call_groq_chat(system_prompt, user_msg, max_tokens=1500, temperature=0.3, deadline=None):
    """
    Hàm gọi Groq API — TẦNG DỊCH DỰ PHÒNG SỐ 2 (khi Gemini quá tải/lỗi).
    - model: openai/gpt-oss-120b (xem ghi chú ở đầu file vì sao đổi model)
    - temperature thấp (0.3): bản dịch ổn định, ít "sáng tạo lệch nghĩa"
    - reasoning_effort: "low" vì dịch thuật không cần suy luận sâu, giúp
      trả lời nhanh hơn trên model dạng reasoning như gpt-oss.
    Trả về chuỗi đã dịch, hoặc None nếu thất bại (để hàm gọi tự quyết định
    phương án dự phòng cuối cùng là Google Translate).

    XOAY VÒNG NHIỀU KEY: giống Gemini ở trên — lần lượt thử từng key
    trong GROQ_API_KEYS, bỏ qua key hỏng/quá tải để thử key kế tiếp.
    `deadline`: xem ghi chú ở _call_gemini_chat.
    """
    if not GROQ_API_KEYS:
        return None

    # CIRCUIT BREAKER: giống Gemini — nếu Groq đã bị ngắt mạch trước đó
    # trong CÙNG request, bỏ qua thẳng, rơi thẳng xuống Google Translate.
    if _cb_is_tripped("groq"):
        print("Groq: mạch đã bị NGẮT do lỗi liên tiếp trước đó trong request này -> bỏ qua, chuyển thẳng sang Google Translate...")
        return None

    for idx, api_key in enumerate(GROQ_API_KEYS):
        key_label = f"key #{idx + 1}/{len(GROQ_API_KEYS)}"

        if deadline is not None and time.time() >= deadline:
            print(f"Groq: hết ngân sách thời gian trước khi thử {key_label} -> chuyển sang Google Translate...")
            return None

        remaining = TIMEOUT_PER_CALL
        if deadline is not None:
            remaining = max(1, min(TIMEOUT_PER_CALL, deadline - time.time()))

        try:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
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
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=remaining)
            if resp.status_code == 200:
                data = resp.json()
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason", "")
                translated = choice["message"]["content"].strip()

                if finish_reason == "length":
                    print("Groq bị CẮT CỤT do hết ngân sách token (finish_reason=length) -> "
                          "loại bỏ kết quả dở dang, chuyển sang Google Translate...")
                    return None

                _cb_record_success("groq")
                return _strip_wrapping_quotes(translated)
            elif resp.status_code == 429:
                if _cb_record_failure("groq"):
                    print("Groq: đã ngắt mạch sau lỗi 429 liên tiếp -> dừng thử các key còn lại, chuyển sang Google Translate...")
                    return None
                print(f"Groq ({key_label}) lỗi 429 (quá tải hạn mức) -> thử key kế tiếp...")
                continue
            elif resp.status_code in _DEAD_KEY_STATUS_CODES:
                print(f"Groq API ({key_label}) lỗi {resp.status_code} (key hỏng/bị khoá) -> thử key kế tiếp...")
                continue
            elif resp.status_code in _OVERLOAD_STATUS_CODES:
                print(f"Groq ({key_label}) quá tải/lỗi tạm thời ({resp.status_code}) -> thử key kế tiếp...")
                continue
            else:
                print(f"Groq API ({key_label}) lỗi {resp.status_code}: {resp.text[:300]}")
        except requests.exceptions.Timeout:
            print(f"Groq API ({key_label}) timeout -> bỏ qua các key Groq còn lại, chuyển thẳng sang Google Translate...")
            _cb_record_failure("groq")
            break
        except Exception as e:
            print(f"Lỗi gọi Groq API ({key_label}): {str(e)}")
            continue

    print("Tất cả Groq API key đều thất bại -> chuyển sang Google Translate...")
    return None


def _call_ai_chat(system_prompt, user_msg, max_tokens=1500, temperature=0.3):
    """
    ĐIỀU PHỐI CHUỖI DỊCH AI: Gemini (1) -> Groq (2).
    Đây là hàm DUY NHẤT mà các hàm dịch thuật khác nên gọi để lấy bản dịch
    từ AI. Nếu hàm này trả None nghĩa là cả 2 AI đều thất bại (hoặc hết
    ngân sách thời gian chung), hàm gọi cần tự rơi về Google Translate
    (free_translate / _translate_plain đã có sẵn logic này).

    QUAN TRỌNG: dùng deadline DÙNG CHUNG CHO CẢ REQUEST (không phải riêng
    cho lệnh gọi này) — xem _get_request_ai_deadline(). Nhờ vậy dù 1 đoạn
    văn dài bị chia thành hàng chục chunk nhỏ, dịch tuần tự, TỔNG thời
    gian dành cho Gemini+Groq trên toàn bộ request vẫn không bao giờ vượt
    quá AI_TOTAL_TIME_BUDGET giây -> không còn nguy cơ "WORKER TIMEOUT".
    """
    deadline = _get_request_ai_deadline()

    translated = _call_gemini_chat(system_prompt, user_msg, max_tokens=max_tokens,
                                    temperature=temperature, deadline=deadline)
    if translated:
        return translated

    translated = _call_groq_chat(system_prompt, user_msg, max_tokens=max_tokens,
                                  temperature=temperature, deadline=deadline)
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


def _translate_plain_single_chunk_ai_only(text, source_lang, target_lang, context=None):
    """
    LÕI AI-ONLY: dịch MỘT khối text thuần qua Gemini -> Groq, trả về chuỗi
    đã dịch hoặc None nếu CẢ HAI đều thất bại — KHÔNG tự gọi Google
    Translate nội bộ.
    Lý do tách riêng: các hàm Smart Batching (_batch_translate_via_ai...)
    cần 1 lõi "chỉ thử AI, thất bại thì trả None" để item thất bại được
    gộp lại và xử lý THỐNG NHẤT bởi tầng Parallel Google Translate Fallback
    (_parallel_google_translate) ở cấp điều phối cao hơn — tránh tình
    huống mỗi item tự âm thầm gọi Google Translate TUẦN TỰ theo kiểu cũ
    ngay bên trong đệ quy chia nhỏ batch, làm mất tác dụng của việc chạy
    song song.
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
    return _call_ai_chat(system_prompt, user_msg, max_tokens=estimated_tokens)


def _translate_plain_single_chunk(text, source_lang, target_lang, context=None):
    """
    Dịch MỘT khối text thuần (không HTML), đã được đảm bảo đủ ngắn để
    không bị cắt cụt — chuỗi 3 tầng: Gemini AI -> Groq AI -> Google
    Translate (tuần tự, vì đây CHỈ có đúng 1 item nên không có gì để chạy
    song song). Đây là hàm "nguyên tử" dùng cho các lời gọi TRỰC TIẾP,
    ĐƠN LẺ bên ngoài cơ chế Smart Batching (vd 1 câu đơn không cần chia
    nhỏ) — KHÔNG dùng hàm này bên trong logic đệ quy của batching, vì nó
    tự xử lý fallback Google Translate ngay tại chỗ thay vì nhường lại
    cho tầng Parallel Google Translate Fallback ở cấp điều phối cao hơn.
    """
    if not text or not text.strip():
        return text

    translated = _translate_plain_single_chunk_ai_only(text, source_lang, target_lang, context=context)
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


# ------------------------------------------------------------
# SMART BATCHING: gộp nhiều chunk ngắn thành 1 lệnh gọi AI DUY NHẤT
# ------------------------------------------------------------
# VẤN ĐỀ CŨ: 1 đoạn văn dài bị chia thành N câu/dòng, rồi dịch TUẦN TỰ
# từng câu (N lệnh gọi Gemini/Groq riêng biệt) -> N * TIMEOUT_PER_CALL là
# độ trễ TỐI ĐA cộng dồn, dễ vượt ngưỡng WORKER TIMEOUT khi N lớn.
# GIẢI PHÁP: gộp N câu thành 1 mảng JSON, gửi 1 LỆNH GỌI AI DUY NHẤT với
# yêu cầu trả về JSON array cùng thứ tự/cùng số lượng phần tử, rồi parse
# ngược lại. Nhờ vậy 1 đoạn dài chỉ tốn ĐÚNG 1 (hoặc vài, nếu quá nhiều
# câu) lệnh gọi thay vì N lệnh gọi.
def _strip_code_fences(text):
    """Bỏ khối ```json ... ``` (hoặc ``` ... ```) nếu AI lỡ bọc quanh JSON,
    để json.loads() không bị lỗi."""
    if not text:
        return text
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
        t = re.sub(r'\s*```$', '', t)
    return t.strip()


# Giới hạn số câu/chunk gộp vào 1 lệnh gọi AI DUY NHẤT. Không gộp vô hạn
# vì: (1) prompt quá dài vẫn có thể khiến model bị cắt cụt hoặc chậm hơn
# TIMEOUT_PER_CALL cho phép, (2) 1 lô nhỏ hơn giúp circuit breaker/deadline
# phản ứng nhanh hơn nếu provider đang gặp sự cố.
SMART_BATCH_MAX_ITEMS_PER_CALL = 12


def _batch_translate_via_ai(texts, source_lang, target_lang, context=None):
    """Gộp NHIỀU đoạn text ĐỘC LẬP thành 1 mảng JSON, gửi 1 lệnh gọi AI
    DUY NHẤT (Gemini -> Groq, có Circuit Breaker + timeout chặt) yêu cầu
    trả về JSON array cùng thứ tự/cùng số lượng phần tử.
    Trả về list cùng độ dài với `texts`: bản dịch (str) nếu hợp lệ, hoặc
    None nếu AI thất bại/trả sai định dạng/sai số lượng phần tử (cần rơi
    xuống tầng dự phòng)."""
    if not texts:
        return []
    if len(texts) == 1:
        return [_translate_plain_single_chunk_ai_only(texts[0], source_lang, target_lang, context=context)]

    src_label = "tiếng Anh" if source_lang == "en" else "tiếng Việt"
    tgt_label = "tiếng Việt" if target_lang == "vi" else "tiếng Anh"

    system_prompt = (
        f"Bạn là chuyên gia dịch thuật {src_label}-{tgt_label} chuyên nghiệp cho "
        "một ứng dụng học tiếng Anh giao tiếp. Bạn sẽ nhận một mảng JSON gồm "
        "nhiều đoạn văn bản ĐỘC LẬP, mỗi phần tử là 1 câu/đoạn cần dịch RIÊNG "
        f"biệt. Hãy dịch TỪNG phần tử sang {tgt_label} một cách tự nhiên, "
        "mượt mà, đúng ngữ cảnh, đúng sắc thái hội thoại đời thường — KHÔNG "
        "dịch máy móc từng từ. Nếu có ngữ cảnh hội thoại đi kèm bên dưới, "
        "hãy dùng nó để dịch đúng ý các câu trả lời ngắn/mơ hồ (ví dụ 'yes, "
        "they do' nên dịch theo đúng ý xác nhận câu hỏi trước đó, KHÔNG dịch "
        "chung chung thành 'tôi đồng ý'). "
        "QUY TẮC BẮT BUỘC khi trả lời: CHỈ trả về DUY NHẤT một mảng JSON "
        "(JSON array) các chuỗi — GIỮ NGUYÊN đúng THỨ TỰ và ĐÚNG SỐ LƯỢNG "
        "phần tử như mảng đầu vào (không gộp 2 phần tử làm 1, không tách "
        "thêm, không bỏ sót). Không thêm giải thích, không thêm markdown, "
        "không thêm bất kỳ văn bản nào khác ngoài mảng JSON."
    ) + _build_context_block(context, "en" if source_lang == "en" else "vi", "vi" if target_lang == "vi" else "en")

    user_payload = json.dumps(texts, ensure_ascii=False)
    user_msg = f"Mảng JSON gồm {len(texts)} phần tử cần dịch (giữ đúng thứ tự, đúng số lượng):\n{user_payload}"

    estimated_tokens = max(900, sum(len(t.split()) for t in texts) * 4 + 400)
    raw = _call_ai_chat(system_prompt, user_msg, max_tokens=estimated_tokens)
    if not raw:
        return [None] * len(texts)

    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        print(f"[Smart Batching] AI trả về không phải JSON hợp lệ ({str(e)}) -> fallback.")
        return [None] * len(texts)

    if not isinstance(parsed, list) or len(parsed) != len(texts):
        got = len(parsed) if isinstance(parsed, list) else "không phải mảng"
        print(f"[Smart Batching] AI trả về sai số lượng phần tử ({got} / {len(texts)}) -> fallback.")
        return [None] * len(texts)

    return [_strip_wrapping_quotes(str(p)) if p not in (None, "") else None for p in parsed]


def _batch_translate_with_retry(texts, source_lang, target_lang, context=None, _depth=0):
    """Gọi _batch_translate_via_ai; nếu THẤT BẠI TOÀN BỘ lô (AI lỗi/JSON
    sai định dạng), thử CHIA ĐÔI lô và gọi lại đệ quy (lô nhỏ hơn dễ dịch
    đúng định dạng hơn) — tối đa 2 lần chia để không tự tạo thêm quá nhiều
    lệnh gọi. Phần tử nào vẫn thất bại sau cùng được để lại None, sẽ do
    tầng Google Translate SONG SONG xử lý ở bước sau."""
    if not texts:
        return []

    results = _batch_translate_via_ai(texts, source_lang, target_lang, context=context)
    if all(r is not None for r in results):
        return results
    if len(texts) == 1 or _depth >= 2:
        return results

    mid = len(texts) // 2
    left = _batch_translate_with_retry(texts[:mid], source_lang, target_lang, context=context, _depth=_depth + 1)
    right = _batch_translate_with_retry(texts[mid:], source_lang, target_lang, context=context, _depth=_depth + 1)
    return left + right


def _translate_chunks_smart(texts, source_lang, target_lang, context=None):
    """
    ĐIỀU PHỐI dịch nhiều chunk văn bản ĐỘC LẬP theo đúng 3 yêu cầu:
      1) Gộp chunk (Smart Batching): TOÀN BỘ các chunk được gộp vào 1 (hoặc
         vài, nếu vượt SMART_BATCH_MAX_ITEMS_PER_CALL) lệnh gọi AI DUY
         NHẤT, thay vì N lệnh gọi tuần tự.
      2) Circuit Breaker + Timeout chặt: đã tích hợp sẵn bên trong
         _call_gemini_chat/_call_groq_chat (deadline dùng chung + ngắt
         mạch sau 2 lỗi Timeout/429 liên tiếp).
      3) Parallel Google Translate Fallback: chunk nào AI không dịch được
         (None) sẽ được đẩy đồng loạt sang Google Translate chạy SONG SONG
         (ThreadPoolExecutor), KHÔNG dịch tuần tự.
    Trả về list bản dịch cùng thứ tự, cùng độ dài với `texts`.
    """
    if not texts:
        return []
    if len(texts) == 1:
        return [_translate_plain_single_chunk(texts[0], source_lang, target_lang, context=context)]

    # Chia thành các lô tối đa SMART_BATCH_MAX_ITEMS_PER_CALL phần tử/lô
    batches = [texts[i:i + SMART_BATCH_MAX_ITEMS_PER_CALL]
               for i in range(0, len(texts), SMART_BATCH_MAX_ITEMS_PER_CALL)]

    all_results = []
    for batch in batches:
        all_results.extend(_batch_translate_with_retry(batch, source_lang, target_lang, context=context))

    missing_idx = [i for i, r in enumerate(all_results) if not r]
    if missing_idx:
        print(f"[Smart Translate] {len(missing_idx)}/{len(texts)} chunk AI không dịch được "
              f"-> fallback Google Translate SONG SONG (ThreadPoolExecutor)...")
        fallback_texts = [texts[i] for i in missing_idx]
        fallback_results = _parallel_google_translate(fallback_texts, source_lang, target_lang)
        for pos, i in enumerate(missing_idx):
            all_results[i] = fallback_results[pos]

    return [r if r else texts[i] for i, r in enumerate(all_results)]


def _translate_plain(text, source_lang, target_lang, groq_key=None, context=None):
    """
    Dịch một đoạn text thuần (không HTML) — TỰ ĐỘNG CHIA NHỎ nếu đoạn quá
    dài (nhiều câu). Khác với bản trước đây (dịch TUẦN TỰ từng câu qua
    Gemini -> Groq -> Google Translate), giờ đây TOÀN BỘ các câu được gộp
    vào 1 (hoặc vài) lệnh gọi AI DUY NHẤT (Smart Batching), và câu nào AI
    thất bại được fallback Google Translate SONG SONG — xem
    _translate_chunks_smart().
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

    translated_pieces = _translate_chunks_smart(sentence_chunks, source_lang, target_lang, context=context)
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


def _batch_translate_marked_texts(marked_list, tag_maps, source_lang, target_lang, context=None):
    """Biến thể Smart Batching DÀNH RIÊNG cho các đoạn có thẻ số định dạng
    (marked_text, vd '<1>bold</1> text <2/>'): gộp NHIỀU đoạn vào 1 mảng
    JSON, gửi AI dịch trong 1 lệnh gọi DUY NHẤT, kèm quy tắc bắt buộc giữ
    nguyên các thẻ số. Trả về list: bản dịch hợp lệ (str, đã giữ đủ thẻ
    số) hoặc None nếu AI thất bại / làm hỏng thẻ số quá nhiều -> cần
    fallback riêng cho phần tử đó."""
    if not marked_list:
        return []
    if len(marked_list) == 1:
        t = _translate_marked_text(marked_list[0], source_lang, target_lang, context=context)
        if t and _verify_markers_intact(t, tag_maps[0]):
            return [t]
        return [None]

    tag_rule = (
        "Mỗi phần tử có thể chứa các thẻ đánh dấu định dạng dạng số, ví dụ "
        "<1>...</1> (chữ đậm/nghiêng/gạch ngang/gạch chân) hoặc <2/> (ngắt "
        "dòng). QUY TẮC BẮT BUỘC: giữ NGUYÊN các thẻ số này trong bản dịch "
        "của TỪNG phần tử — không đổi số, không thêm/xoá thẻ, không dịch "
        "hay sửa đổi ký tự bên trong dấu < >; được phép di chuyển 1 thẻ "
        "sang đúng vị trí từ/cụm từ tương ứng trong bản dịch nếu trật tự "
        "từ của ngôn ngữ đích khác câu gốc, miễn cặp mở/đóng cùng số vẫn "
        "bao đúng phần nội dung mà nó nhấn mạnh."
    )
    src_label = "tiếng Anh" if source_lang == "en" else "tiếng Việt"
    tgt_label = "tiếng Việt" if target_lang == "vi" else "tiếng Anh"

    system_prompt = (
        f"Bạn là chuyên gia dịch thuật {src_label}-{tgt_label} chuyên nghiệp, dịch tự "
        "nhiên như người bản ngữ viết. Bạn sẽ nhận 1 mảng JSON gồm nhiều đoạn "
        f"văn bản ĐỘC LẬP, dịch TỪNG phần tử sang {tgt_label}. " + tag_rule +
        " QUY TẮC BẮT BUỘC khi trả lời: CHỈ trả về DUY NHẤT 1 mảng JSON các "
        "chuỗi, GIỮ ĐÚNG THỨ TỰ và ĐÚNG SỐ LƯỢNG phần tử như mảng đầu vào, "
        "không thêm giải thích, không thêm markdown."
    ) + _build_context_block(context, "en" if source_lang == "en" else "vi", "vi" if target_lang == "vi" else "en")

    user_payload = json.dumps(marked_list, ensure_ascii=False)
    user_msg = f"Mảng JSON gồm {len(marked_list)} phần tử cần dịch, giữ nguyên các thẻ số:\n{user_payload}"

    estimated_tokens = max(1000, sum(len(t.split()) for t in marked_list) * 4 + 500)
    raw = _call_ai_chat(system_prompt, user_msg, max_tokens=estimated_tokens)
    if not raw:
        return [None] * len(marked_list)

    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        print(f"[Smart Batching - HTML] AI trả về không phải JSON hợp lệ ({str(e)}) -> fallback.")
        return [None] * len(marked_list)

    if not isinstance(parsed, list) or len(parsed) != len(marked_list):
        got = len(parsed) if isinstance(parsed, list) else "không phải mảng"
        print(f"[Smart Batching - HTML] AI trả về sai số lượng phần tử ({got}/{len(marked_list)}) -> fallback.")
        return [None] * len(marked_list)

    out = []
    for text, tmap in zip(parsed, tag_maps):
        text = _strip_wrapping_quotes(str(text)) if text not in (None, "") else None
        out.append(text if (text and _verify_markers_intact(text, tmap)) else None)
    return out


def _batch_translate_marked_with_retry(marked_list, tag_maps, source_lang, target_lang, context=None, _depth=0):
    """Giống _batch_translate_with_retry nhưng cho biến thể marked_text:
    nếu cả lô thất bại, chia đôi và thử lại đệ quy (tối đa 2 lần)."""
    if not marked_list:
        return []
    results = _batch_translate_marked_texts(marked_list, tag_maps, source_lang, target_lang, context=context)
    if all(r is not None for r in results):
        return results
    if len(marked_list) == 1 or _depth >= 2:
        return results
    mid = len(marked_list) // 2
    left = _batch_translate_marked_with_retry(marked_list[:mid], tag_maps[:mid], source_lang, target_lang, context=context, _depth=_depth + 1)
    right = _batch_translate_marked_with_retry(marked_list[mid:], tag_maps[mid:], source_lang, target_lang, context=context, _depth=_depth + 1)
    return left + right


def translate_preserving_html(html_text, source_lang, target_lang, context=None):
    """
    Dịch nội dung HTML giữ nguyên định dạng (bold, italic, strike, br...).

    CHIẾN LƯỢC (Smart Batching thay cho dịch tuần tự từng dòng):
    Toàn bộ nội dung được TÁCH THEO TỪNG DÒNG/ĐOẠN (ngăn cách bởi <br>).
    Thay vì gọi AI RIÊNG cho từng dòng (N dòng = N lệnh gọi tuần tự — vốn
    là nguyên nhân chính gây cộng dồn độ trễ dẫn tới WORKER TIMEOUT), các
    dòng được tách thành 2 nhóm và GỘP LẠI:
      - Nhóm KHÔNG có định dạng (không thẻ <b>/<i>/<s>/<u>): gộp thành 1
        lô Smart Batching thẳng (mỗi dòng dài tự tách câu bên trong).
      - Nhóm CÓ định dạng: gộp thành 1 lô Smart Batching biến thể GIỮ
        NGUYÊN thẻ số (_batch_translate_marked_with_retry).
    Mỗi nhóm chỉ tốn 1 (hoặc vài, nếu vượt SMART_BATCH_MAX_ITEMS_PER_CALL)
    lệnh gọi AI cho TOÀN BỘ các dòng cùng loại, thay vì N lệnh gọi riêng.
    Dòng nào AI thất bại/làm hỏng thẻ số được fallback dịch riêng SONG
    SONG (ThreadPoolExecutor) thay vì tuần tự, để không bao giờ vỡ layout
    và không cộng dồn độ trễ.
    """
    segments = parse_html_segments(html_text)
    if not segments:
        return free_translate(html_text, source_lang, target_lang)

    merged = _merge_segments(segments)
    paragraphs = _split_merged_into_paragraphs(merged)

    # Bước 1: thu thập TOÀN BỘ dòng cần dịch, tách thành 2 nhóm (thường/
    # có định dạng), giữ lại thông tin cần cho bước fallback nếu cần.
    para_infos = [None] * len(paragraphs)
    plain_idx, plain_texts = [], []
    tagged_idx, tagged_texts, tagged_maps = [], [], []

    for i, item in enumerate(paragraphs):
        if item["type"] == "br":
            continue
        chunks = item["chunks"]
        plain_preview = "".join(c["text"] for c in chunks)
        if not plain_preview.strip():
            continue
        marked_text, tag_map = _build_marked_text_from_chunks(chunks)
        para_infos[i] = {"marked_text": marked_text, "tag_map": tag_map, "chunks": chunks}
        if not tag_map:
            plain_idx.append(i)
            plain_texts.append(marked_text)
        else:
            tagged_idx.append(i)
            tagged_texts.append(marked_text)
            tagged_maps.append(tag_map)

    translated_by_idx = {}

    # Bước 2: NHÓM KHÔNG định dạng -> Smart Batching thẳng
    if plain_texts:
        flat_pieces, span = [], []
        for t in plain_texts:
            pieces = _split_into_sentences(t, max_chars=350) or [t]
            start = len(flat_pieces)
            flat_pieces.extend(pieces)
            span.append((start, len(pieces)))
        flat_translated = _translate_chunks_smart(flat_pieces, source_lang, target_lang, context=context)
        for (start, count), i in zip(span, plain_idx):
            translated_by_idx[i] = " ".join(p for p in flat_translated[start:start + count] if p)

    # Bước 3: NHÓM CÓ định dạng -> Smart Batching giữ thẻ số
    if tagged_texts:
        tagged_results = _batch_translate_marked_with_retry(tagged_texts, tagged_maps, source_lang, target_lang, context=context)
        fail_positions = [pos for pos, r in enumerate(tagged_results) if r is None]

        if fail_positions:
            print(f"[Smart Batching - HTML] {len(fail_positions)}/{len(tagged_texts)} dòng có định dạng "
                  f"bị AI làm hỏng thẻ số hoặc lỗi -> fallback dịch riêng SONG SONG cho các dòng này...")

            def _fallback_one(pos):
                i = tagged_idx[pos]
                info = para_infos[i]
                fallback_segments = [{"text": c["text"], "tags": c["tags"], "is_br": False} for c in info["chunks"]]
                html_out = _translate_preserving_html_fallback(fallback_segments, source_lang, target_lang, context=context)
                return i, html_out

            max_workers = max(1, min(GOOGLE_TRANSLATE_MAX_WORKERS, len(fail_positions)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_fallback_one, pos) for pos in fail_positions]
                for future in as_completed(futures):
                    i, html_out = future.result()
                    translated_by_idx[i] = html_out   # đã là HTML hoàn chỉnh (tags áp lại sẵn)

        fail_set = {tagged_idx[pos] for pos in fail_positions}
        for pos, i in enumerate(tagged_idx):
            if i in fail_set:
                continue
            translated_by_idx[i] = _apply_marked_translation_to_html(tagged_results[pos], tagged_maps[pos])

    # Bước 4: ghép lại đúng thứ tự gốc
    result_parts = []
    for i, item in enumerate(paragraphs):
        if item["type"] == "br":
            result_parts.append("<br>")
        elif i in translated_by_idx:
            result_parts.append(translated_by_idx[i])

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
    """Dịch một câu/1 dòng — TỰ ĐỘNG chia nhỏ theo câu nếu quá dài. Nếu chỉ
    có 1 câu, dùng đường dịch "atomic" (giữ nguyên prompt xử lý câu trả
    lời ngắn theo ngữ cảnh, vd 'yes, they do'). Nếu nhiều câu, gộp TẤT CẢ
    vào Smart Batching (1 lệnh gọi AI duy nhất) thay vì dịch tuần tự."""
    if not text or not text.strip():
        return text

    sentence_chunks = _split_into_sentences(text, max_chars=350)
    if len(sentence_chunks) <= 1:
        return _translate_sentence_atomic(text, source_lang, target_lang, context=context)

    translated_pieces = _translate_chunks_smart(sentence_chunks, source_lang, target_lang, context=context)
    return " ".join(p for p in translated_pieces if p)


def _translate_lines_smart(lines, source_lang, target_lang, context=None):
    """Dịch NHIỀU DÒNG cùng lúc bằng Smart Batching.
    Trước đây: mỗi dòng được dịch qua _translate_sentence RIÊNG (dòng dài
    lại tự chia câu bên trong) -> tổng số lệnh gọi AI = tổng số câu của
    TẤT CẢ các dòng, dịch TUẦN TỰ.
    Bây giờ: mỗi dòng được tách sẵn thành các câu (nếu cần), rồi TOÀN BỘ
    câu của TẤT CẢ các dòng được gộp chung vào 1 lô Smart Batching duy
    nhất (_translate_chunks_smart) — chỉ còn 1 (hoặc vài, nếu vượt
    SMART_BATCH_MAX_ITEMS_PER_CALL) lệnh gọi AI cho CẢ ĐOẠN nhiều dòng.
    Trả về list bản dịch từng dòng, cùng thứ tự với `lines` (dòng trống
    trả về chuỗi rỗng)."""
    flat_pieces = []
    line_span = []  # (start_index_trong_flat_pieces, số_câu) cho từng dòng
    for line in lines:
        if not line.strip():
            line_span.append((len(flat_pieces), 0))
            continue
        pieces = _split_into_sentences(line, max_chars=350) or [line]
        start = len(flat_pieces)
        flat_pieces.extend(pieces)
        line_span.append((start, len(pieces)))

    translated_flat = _translate_chunks_smart(flat_pieces, source_lang, target_lang, context=context)

    translated_lines = []
    for start, count in line_span:
        if count == 0:
            translated_lines.append("")
        else:
            translated_lines.append(" ".join(p for p in translated_flat[start:start + count] if p))
    return translated_lines


def smart_translate(text, source_lang, target_lang, context=None):
    """Dịch thông minh — tự động giữ định dạng HTML nếu có, luôn dịch theo
    ngữ cảnh nguyên câu (và ngữ cảnh hội thoại trước đó nếu có) thay vì
    rời rạc từng từ. Chuỗi dịch: Gemini AI -> Groq AI -> Google Translate.
    Nội dung nhiều dòng/nhiều câu được TỰ ĐỘNG CHIA NHỎ (theo dòng, rồi
    theo câu nếu vẫn còn dài) và GỘP LẠI thành Smart Batching để dịch
    TOÀN BỘ trong tối thiểu số lệnh gọi AI, không bao giờ dừng lại giữa
    chừng."""
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

    # Nhiều dòng -> gộp TẤT CẢ câu của TẤT CẢ dòng vào Smart Batching,
    # dịch chung trong tối thiểu số lệnh gọi AI, rồi ghép lại đúng bố cục
    # xuống dòng gốc.
    translated_lines = _translate_lines_smart(lines, source_lang, target_lang, context=context)
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


@app.route('/api/sync_all_cache', methods=['GET'])
def api_sync_all_cache():
    """
    "Đánh thức" server này và ép nó tự làm mới TOÀN BỘ cache cục bộ
    (danh sách user + mọi file nhật ký học) theo đúng bản mới nhất trên
    GitHub — kể cả khi KHÔNG có ai thật sự đăng nhập/mở app.

    LÝ DO CÓ ENDPOINT NÀY: cache cục bộ trước đây chỉ được làm mới khi
    có người dùng thật sự truy cập ĐÚNG server đó (xem load_users_db,
    load_data). Nếu cả nhà chỉ dùng URL Render, cache trên PythonAnywhere
    sẽ không tự cập nhật cho tới khi có ai mở đúng URL PythonAnywhere.
    Endpoint này cho phép dùng 1 dịch vụ ping/cron MIỄN PHÍ bên ngoài
    (UptimeRobot, cron-job.org, GitHub Actions cron...) gọi định kỳ (ví
    dụ mỗi 10-15 phút) tới URL này của CẢ 2 server -> biến cả 2 thành
    "bản sao lưu nóng" luôn cập nhật, sẵn sàng dùng ngay nếu server kia
    hoặc GitHub gặp sự cố, mà không cần tính năng Scheduled Task trả phí
    của PythonAnywhere.

    Chỉ đọc + ghi cache cục bộ, không trả về nội dung dữ liệu thật (chỉ
    trả về số lượng) nên an toàn để không cần bảo vệ bằng mật khẩu.
    """
    users_db = load_users_db()   # tự ghi cache cục bộ (write-through đã có)
    synced_logs = 0
    filenames = _github_list_dir(GITHUB_DATA_PATH) if _github_storage_configured() else None
    if filenames:
        for fname in filenames:
            if fname.startswith('learning_log_') and fname.endswith('.json'):
                uname = fname[len('learning_log_'):-len('.json')]
                load_data(uname)   # tự ghi cache cục bộ (write-through đã có)
                synced_logs += 1
    return jsonify({
        "status": "success",
        "synced_users_count": len(users_db),
        "synced_logs_count": synced_logs
    })


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
        stored_password = user_info.get('password', '')
        if _verify_user_password(stored_password, password):
            # Tự động nâng cấp mật khẩu plaintext cũ (từ trước khi có hash)
            # thành dạng đã hash ngay khi xác thực thành công, để dữ liệu
            # dần được vá mà không cần admin phải đổi lại từng mật khẩu.
            if not _is_hashed(stored_password):
                users_db[sys_username]['password'] = _hash_password(password)
                save_users_db(users_db)
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
        "password": _hash_password(new_password),
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

    users_db[target_user]['password'] = _hash_password(new_password)
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
    # Mở ngân sách thời gian DÙNG CHUNG cho toàn bộ request này (mọi
    # chunk/dòng/câu bên trong text đều dùng chung deadline này) -> xem
    # ghi chú chi tiết tại _start_request_ai_deadline() / AI_TOTAL_TIME_BUDGET.
    _start_request_ai_deadline()

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