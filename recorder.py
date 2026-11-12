import glob
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8')

# Dynamic Configuration
CHANNEL_HANDLE = os.environ.get("CHANNEL_HANDLE", sys.argv[1] if len(sys.argv) > 1 else "riktorrr")
HOURS = float(os.environ.get("HOURS", "4.5"))
OUT_DIR = os.environ.get("OUT_DIR", "./streams")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "./cookies.txt")
REQUIRE_COOKIES = True

CHECK_SECONDS = 30
HEARTBEAT_SECONDS = 15
STALL_SECONDS = 180
FAILURE_COOLDOWN_SECONDS = 45
MIN_SAVE_BYTES = 1_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

BLOCK_MARKERS = ["cloudflare", "captcha", "forbidden", "http error 403", "403"]
AUTH_MARKERS = ["login", "unauthorized", "401", "mature", "age", "restricted"]
NOT_LIVE_MARKERS = ["not currently live", "channel is offline", "offline", "404: not found"]

def slug_channel(value):
    value = str(value or "").strip().rstrip("/")
    if value.startswith("http://") or value.startswith("https://"):
        value = value.split("/")[-1]
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_").lower()

CHANNEL_SLUG = slug_channel(CHANNEL_HANDLE)
CHANNEL_URL = f"https://kick.com/{CHANNEL_SLUG}"
LOG_PATH = os.path.join(OUT_DIR, f"{CHANNEL_SLUG}_kick_session.log")
SUMMARY_PATH = os.path.join(OUT_DIR, f"{CHANNEL_SLUG}_kick_summary.json")

os.makedirs(OUT_DIR, exist_ok=True)

def log(message):
    now_ist = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")
    line = f"[{now_ist} IST] {message}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass

def log_raw(prefix, message):
    text = str(message or "").rstrip()
    if text:
        log(f"{prefix} | {text}")

def hms(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

SUMMARY = {
    "platform": "kick",
    "channel": CHANNEL_HANDLE,
    "channel_url": CHANNEL_URL,
    "cookies_file": None,
    "status": "init",
    "failure_reason": None,
    "recorded_files": [],
    "total_size_mb": 0.0,
}

def write_summary(**updates):
    SUMMARY.update(updates)
    try:
        with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
            json.dump(SUMMARY, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

try:
    from curl_cffi import requests
except Exception:
    requests = None

def resolve_cookies_file():
    candidates = []
    if COOKIES_FILE:
        candidates.append(COOKIES_FILE)

    candidates += [
        "./cookies.txt",
        "./kick_cookies.txt",
        "../cookies.txt",
        "/tmp/cookies.txt"
    ]
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return os.path.abspath(path)
    return None

RESOLVED_COOKIES_FILE = resolve_cookies_file()
write_summary(cookies_file=RESOLVED_COOKIES_FILE)

if RESOLVED_COOKIES_FILE:
    log(f"cookies_file | found | {RESOLVED_COOKIES_FILE}")
else:
    log("cookies_file | not found | guest mode")

def cookie_header_from_netscape(path):
    if not path or not os.path.exists(path):
        return ""

    pairs = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, _cookie_path, _secure, _expiry, name, value = parts[:7]
                if "kick.com" in domain.lower() and name and value:
                    pairs.append(f"{name}={value}")
    except Exception as exc:
        log(f"cookies_parse_error | {exc}")
        return ""

    return "; ".join(pairs)

COOKIE_HEADER = cookie_header_from_netscape(RESOLVED_COOKIES_FILE)

if RESOLVED_COOKIES_FILE:
    log(f"cookies_header | {'ready_for_kick' if COOKIE_HEADER else 'file_found_but_no_kick_cookies'}")
elif REQUIRE_COOKIES:
    log("cookies_required_missing | cookies.txt is required for Kick recording")
    write_summary(status="cookies_required_missing", failure_reason="cookies_missing")
    raise SystemExit(1)

def request_json(url):
    if requests is None:
        log("api_error | curl_cffi unavailable")
        return None

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": CHANNEL_URL,
        "Origin": "https://kick.com",
    }
    if COOKIE_HEADER:
        headers["Cookie"] = COOKIE_HEADER

    try:
        response = requests.get(url, impersonate="chrome110", headers=headers, timeout=20)
        log(f"api_probe | {url} | status={response.status_code}")
        if response.status_code != 200:
            log_raw("api_body", response.text[:400].replace("\n", " "))
            return None
        return response.json()
    except Exception as exc:
        log(f"api_error | {url} | {exc}")
        return None

def is_live_data(data):
    if not isinstance(data, dict):
        return False
    livestream = data.get("livestream")
    if not livestream:
        return False
    if isinstance(livestream, dict):
        return bool(livestream.get("is_live", True))
    return True

def extract_playback_url(data):
    if not isinstance(data, dict):
        return None

    direct = data.get("playback_url") or data.get("source") or data.get("url")
    if direct:
        return direct

    livestream = data.get("livestream")
    if isinstance(livestream, dict):
        for key in ("playback_url", "source", "url"):
            if livestream.get(key):
                return livestream[key]
        playback = livestream.get("playback")
        if isinstance(playback, dict):
            for key in ("url", "playback_url", "source"):
                if playback.get(key):
                    return playback[key]

    return None

def live_title(data):
    livestream = data.get("livestream") if isinstance(data, dict) else None
    if isinstance(livestream, dict):
        return livestream.get("session_title") or livestream.get("slug") or ""
    return ""

def api_live_probe():
    for url in (f"https://kick.com/api/v2/channels/{CHANNEL_SLUG}", f"https://kick.com/api/v1/channels/{CHANNEL_SLUG}"):
        data = request_json(url)
        if not data:
            continue
        if not is_live_data(data):
            continue

        playback_url = extract_playback_url(data)
        title = live_title(data)
        log(f"detect_live | source=kick_api | title={title[:80]}")
        if playback_url:
            log(f"playback_url_preview | {playback_url[:120]}")
            return {"urls": [playback_url, CHANNEL_URL], "source": "kick_api"}
        return {"urls": [CHANNEL_URL], "source": "kick_api_no_playback"}

    return None

def ytdlp_common_args():
    args = [
        "--user-agent", USER_AGENT,
        "--referer", CHANNEL_URL,
        "--add-header", f"Referer: {CHANNEL_URL}",
        "--add-header", "Origin: https://kick.com",
    ]
    if RESOLVED_COOKIES_FILE:
        args += ["--cookies", RESOLVED_COOKIES_FILE]
    return args

def ytdlp_live_probe():
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--skip-download",
        "--no-warnings",
        "--print", "before_dl:id=%(id)s | title=%(title)s | is_live=%(is_live)s | live_status=%(live_status)s",
    ] + ytdlp_common_args() + [CHANNEL_URL]

    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=35, encoding="utf-8", errors="ignore")
    except Exception as exc:
        log(f"yt_probe_error | {exc}")
        return None

    output = (result.stdout or "") + (result.stderr or "")
    for line in output.splitlines()[-8:]:
        log_raw("yt_probe", line)

    lowered = output.lower()
    if result.returncode == 0 and ("is_live=true" in lowered or "live_status=is_live" in lowered):
        log("detect_live | source=yt_dlp_page")
        return {"urls": [CHANNEL_URL], "source": "yt_dlp_page"}
    return None

def get_live():
    return ytdlp_live_probe() or api_live_probe()

STOP_REQUESTED = False
CURRENT_PROC = None

def stop_proc(proc):
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

def sigterm_handler(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log("sigterm_received | stopping current download cleanly and saving footage")
    stop_proc(CURRENT_PROC)

signal.signal(signal.SIGTERM, sigterm_handler)
signal.signal(signal.SIGINT, sigterm_handler)

def current_saved_path(base_path):
    if os.path.exists(base_path):
        return base_path
    part_path = base_path + ".part"
    if os.path.exists(part_path):
        return part_path
    return None

def finalize_path(base_path):
    if os.path.exists(base_path):
        return base_path
    part_path = base_path + ".part"
    if os.path.exists(part_path):
        try:
            os.replace(part_path, base_path)
            return base_path
        except Exception:
            return part_path
    return None

def next_output_path():
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%d_%H%MUTC")
    base_name = f"{CHANNEL_SLUG}_{now_utc}"
    candidate = os.path.join(OUT_DIR, f"{base_name}.ts")
    idx = 1
    while os.path.exists(candidate) or os.path.exists(candidate + ".part"):
        candidate = os.path.join(OUT_DIR, f"{base_name}_part{idx}.ts")
        idx += 1
    return candidate

def classify_output(state):
    if state["blocked"]:
        return "blocked_or_403"
    if state["auth"]:
        return "auth_or_age_required"
    if state["not_live"]:
        return "not_live"
    return None

def output_reader(stream, state):
    for raw_line in iter(stream.readline, ""):
        if not raw_line:
            break
        print(raw_line, end="", flush=True)
        lowered = raw_line.lower()
        if "error:" in lowered or "warning:" in lowered or "[download]" in lowered:
            log_raw("yt_dlp", raw_line)
        if any(marker in lowered for marker in BLOCK_MARKERS):
            state["blocked"] = True
        if any(marker in lowered for marker in AUTH_MARKERS):
            state["auth"] = True
        if any(marker in lowered for marker in NOT_LIVE_MARKERS):
            state["not_live"] = True
    try:
        stream.close()
    except Exception:
        pass

def make_download_cmd(url, final_ts):
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--newline",
        "--no-warnings",
        "--restrict-filenames",
        "--retries", "20",
        "--file-access-retries", "20",
        "--fragment-retries", "infinite",
        "--retry-sleep", "http:2",
        "--retry-sleep", "fragment:1",
        "--hls-use-mpegts",
        "--downloader", "ffmpeg",
        "-N", "1",
        "-f", "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
        "-o", final_ts,
    ] + ytdlp_common_args() + [url]
    return cmd

def run_download(url, final_ts, session_end, source_label):
    global CURRENT_PROC

    cmd = make_download_cmd(url, final_ts)
    log(f"[{hms(session_end - time.time())} left] download_started | source={source_label} | url={url[:80]}")
    write_summary(status="download_started", failure_reason=None)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8", errors="ignore")
    CURRENT_PROC = proc

    state = {"blocked": False, "auth": False, "not_live": False}
    reader = threading.Thread(target=output_reader, args=(proc.stdout, state), daemon=True)
    reader.start()

    last_heartbeat = 0
    last_size = -1
    last_growth_at = time.time()

    while proc.poll() is None and time.time() < session_end and not STOP_REQUESTED:
        time.sleep(HEARTBEAT_SECONDS)
        now = time.time()
        path = current_saved_path(final_ts)
        size_bytes = os.path.getsize(path) if path and os.path.exists(path) else 0

        if size_bytes > last_size:
            last_size = size_bytes
            last_growth_at = now

        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            if path and os.path.exists(path):
                size_mb = size_bytes / 1048576
                suffix = " (.part)" if path.endswith(".part") else ""
                log(f"[{hms(session_end - now)} left] heartbeat | {size_mb:.1f} MB{suffix}")
            else:
                log(f"[{hms(session_end - now)} left] heartbeat | waiting for file")
            last_heartbeat = now

        if now - last_growth_at >= STALL_SECONDS:
            log("download_stalled (no growth for 180s) -> restarting stream capture")
            stop_proc(proc)
            break

    if proc.poll() is None:
        stop_proc(proc)

    CURRENT_PROC = None
    reader.join(timeout=5)

    saved = finalize_path(final_ts)
    if not saved or not os.path.exists(saved):
        reason = classify_output(state) or "no_file"
        log(f"download_failed | reason={reason} | rc={proc.returncode}")
        write_summary(status="download_failed", failure_reason=reason)
        return {"ok": False, "reason": reason}

    size_bytes = os.path.getsize(saved)
    size_mb = size_bytes / 1048576
    if size_bytes < MIN_SAVE_BYTES:
        log(f"tiny_file_ignored | {size_mb:.1f} MB")
        try:
            os.remove(saved)
        except Exception:
            pass
        write_summary(status="download_failed", failure_reason="tiny_file")
        return {"ok": False, "reason": "tiny_file"}

    log(f"download_saved | {size_mb:.1f} MB | {os.path.basename(saved)}")
    SUMMARY["recorded_files"].append({
        "file": os.path.basename(saved),
        "size_mb": round(size_mb, 1),
        "path": saved
    })
    SUMMARY["total_size_mb"] = round(sum(f["size_mb"] for f in SUMMARY["recorded_files"]), 1)
    write_summary(status="download_saved", failure_reason=None)
    return {"ok": True, "reason": None}

write_summary(status="detect_none")

session_end = time.time() + HOURS * 3600
next_check = 0
cooldown_until = 0

log(f"==================================================")
log(f"Kick Stream Recorder Started: {CHANNEL_HANDLE}")
log(f"Channel URL: {CHANNEL_URL}")
log(f"Output Directory: {OUT_DIR}")
log(f"Session Duration: {HOURS} hours")
log(f"Cookies: {'Enabled' if RESOLVED_COOKIES_FILE else 'Disabled'}")
log(f"==================================================")

try:
    while time.time() < session_end and not STOP_REQUESTED:
        now = time.time()
        left = session_end - now

        if now < cooldown_until:
            time.sleep(min(5, max(1, int(cooldown_until - now))))
            continue

        if now >= next_check:
            live = get_live()
            next_check = time.time() + CHECK_SECONDS
            if not live:
                log(f"[{hms(left)} left] Channel offline | Next check in {CHECK_SECONDS}s")
                write_summary(status="offline")
                time.sleep(1)
                continue

            final_ts = next_output_path()
            urls = []
            for url in live["urls"]:
                if url and url not in urls:
                    urls.append(url)

            last_result = {"ok": False, "reason": "no_url"}
            for index, url in enumerate(urls, start=1):
                label = f"{live.get('source')}_{index}"
                last_result = run_download(url, final_ts, session_end, label)
                if last_result["ok"] or STOP_REQUESTED or time.time() >= session_end:
                    break
                if last_result["reason"] == "not_live":
                    break

            if not last_result["ok"]:
                cooldown_until = time.time() + FAILURE_COOLDOWN_SECONDS

        time.sleep(1)
finally:
    stop_proc(CURRENT_PROC)

log("Session complete.")
write_summary(status="session_complete")
print(OUT_DIR)
