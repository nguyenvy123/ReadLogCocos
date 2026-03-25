"""
Agent 1: Cocos Log Analyzer
Đọc log từ LDPlayer (qua ADB hoặc file), phân tích và tạo bug reproduction flow.
Hỗ trợ nhiều device cùng lúc, watch mode, pre-filtering và screenshot capture.
"""

import re
import json
import subprocess
import concurrent.futures
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable
import anthropic

client = anthropic.Anthropic()

# ─── Cocos Log Filter Constants ───────────────────────────────────────────────

COCOS_TAGS = {
    "cocos2d", "cocos", "cocoscreator", "jsb", "luaengine", "v8", "scriptengine",
    "audioengine", "cocosrenderer", "director", "scenemanager", "eventdispatcher",
    "scheduler", "resourcemanager", "texturemanager", "texturecache", "spriteframemanager",
    "animationmanager", "physicsmanager", "uimanager", "networkmanager",
    "spine", "dragonbones", "jsengine", "gameactivity", "sdlactivity",
    "cocosplatform", "cocosengine", "cocosapp", "crashhandler", "opengl",
}

ERROR_KEYWORDS = {
    "exception", "fatal", "crash", "assert failed", "assertion", "signal ",
    "outofmemory", " oom", "anr ", "segfault", "sigsegv", "sigabrt", "sigill",
    "null pointer", "nullpointer", "stack trace", "caused by:", "at com.",
    "uncaught exception", "out of memory", "gl_out_of_memory", "gl error",
    "runtime error", "native crash", "abort message",
}

# ─── Tool Definitions ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_log_file",
        "description": "Đọc nội dung file log từ đĩa. Dùng khi log đã được lưu ra file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Đường dẫn tuyệt đối hoặc tương đối tới file log",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Chỉ đọc N dòng cuối. Bỏ trống để đọc toàn bộ.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_adb_logs",
        "description": (
            "Lấy log trực tiếp từ LDPlayer qua ADB (Android Debug Bridge). "
            "Dùng khi LDPlayer đang chạy và ADB được bật."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tag_filter": {
                    "type": "string",
                    "description": "Lọc theo logcat tag. Bỏ trống để lấy tất cả log.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Số dòng log cần lấy (mặc định 1000)",
                },
                "device_serial": {
                    "type": "string",
                    "description": "Serial ADB của device. Bỏ trống để tự phát hiện LDPlayer.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["V", "D", "I", "W", "E", "F"],
                    "description": "Mức priority tối thiểu: V=Verbose, D=Debug, I=Info, W=Warn, E=Error, F=Fatal",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_adb_devices",
        "description": "Liệt kê các thiết bị ADB đang kết nối, bao gồm LDPlayer.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_device_info",
        "description": "Lấy thông tin hệ thống của device (Android version, model, memory...).",
        "input_schema": {
            "type": "object",
            "properties": {
                "device_serial": {
                    "type": "string",
                    "description": "Serial ADB của device",
                }
            },
            "required": ["device_serial"],
        },
    },
]

# ─── Pre-filtering ────────────────────────────────────────────────────────────

def _is_error_level(line: str) -> bool:
    """Kiểm tra dòng log có level E hoặc F không (logcat formats)."""
    # Format 1: "MM-DD HH:MM:SS.mmm PID TID LEVEL TAG: ..."
    parts = line.split()
    if len(parts) >= 5 and parts[4] in ("E", "F"):
        return True
    # Format 2: "E/TAG(pid): ..." hoặc "F/TAG: ..."
    if re.match(r"^[EF][/ ]", line):
        return True
    return False


def pre_filter_logs(raw_log: str, max_lines: int = 2000) -> str:
    """
    Lọc log giữ lại những dòng liên quan đến Cocos và lỗi.
    Giảm đáng kể số token gửi lên Claude AI.

    Giữ lại:
    1. Dòng có level E/F (Error/Fatal)
    2. Dòng có Cocos-related tags
    3. Dòng chứa error keywords
    """
    lines = raw_log.splitlines()
    filtered = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        line_lower = stripped.lower()

        # Error/Fatal level
        if _is_error_level(stripped):
            filtered.append(line)
            continue

        # Cocos tags
        if any(tag in line_lower for tag in COCOS_TAGS):
            filtered.append(line)
            continue

        # Error keywords
        if any(kw in line_lower for kw in ERROR_KEYWORDS):
            filtered.append(line)
            continue

    if not filtered:
        # Không filter được gì — trả về nguyên bản (tránh mất thông tin)
        return raw_log

    if len(filtered) > max_lines:
        filtered = filtered[-max_lines:]  # Lấy N dòng cuối (mới nhất)

    original_count = len(lines)
    header = f"[PRE-FILTERED: {len(filtered)}/{original_count} lines | Cocos+Error only]\n"
    return header + "\n".join(filtered)


def has_critical_errors(log_text: str) -> bool:
    """
    Kiểm tra nhanh xem log có chứa lỗi nghiêm trọng không.
    Không dùng AI — chỉ regex. Dùng trong watch mode.
    """
    patterns = [
        r"\bE/", r"\bF/",               # Error/Fatal ADB levels
        r"\s E\s+\w",                    # logcat long format level E
        r"\s F\s+\w",                    # logcat long format level F
        r"FATAL|CRASH|SIGSEGV|SIGABRT",
        r"java\.lang\.\w+Exception",
        r"signal \d+",
        r"\bANR\b",
        r"GL_OUT_OF_MEMORY",
        r"OutOfMemoryError",
    ]
    for pattern in patterns:
        if re.search(pattern, log_text, re.IGNORECASE):
            return True
    return False

# ─── Tool Implementations ─────────────────────────────────────────────────────

def read_log_file(file_path: str, tail_lines: Optional[int] = None) -> str:
    path = Path(file_path)
    if not path.exists():
        return f"[ERROR] Không tìm thấy file: {file_path}"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if tail_lines and tail_lines > 0:
            lines = lines[-tail_lines:]

        content = "".join(lines)
        size_kb = path.stat().st_size / 1024
        raw = f"[FILE: {path.name} | {size_kb:.1f} KB | {len(lines)} dòng]\n\n{content}"
        return pre_filter_logs(raw)
    except Exception as e:
        return f"[ERROR] Không đọc được file: {e}"


def get_adb_logs(
    tag_filter: str = "",
    lines: int = 1000,
    device_serial: str = "",
    priority: str = "V",
) -> str:
    try:
        if not device_serial:
            devices_result = subprocess.run(
                ["adb", "devices"], capture_output=True, text=True, timeout=10
            )
            device_lines = [
                l.split()[0]
                for l in devices_result.stdout.splitlines()[1:]
                if l.strip() and "device" in l and "offline" not in l
            ]
            if not device_lines:
                return (
                    "[ERROR] Không tìm thấy device ADB nào.\n"
                    "Hãy bật ADB trong LDPlayer: Settings → Others → Open ADB debug."
                )
            ldplayer = next(
                (d for d in device_lines if "5554" in d or "127.0.0.1" in d or "localhost" in d),
                device_lines[0],
            )
            device_serial = ldplayer

        cmd = ["adb", "-s", device_serial, "logcat", "-d", "-t", str(lines)]

        if tag_filter:
            cmd += [f"{tag_filter}:{priority}", "*:S"]
        else:
            cmd += [f"*:{priority}"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            return f"[ERROR] ADB thất bại: {result.stderr}"

        output = result.stdout.strip()
        if not output:
            return f"[INFO] Không có log từ device {device_serial} với filter hiện tại."

        raw = f"[ADB LOGS | device={device_serial} | filter={tag_filter or 'all'} | priority>={priority}]\n\n{output}"
        return pre_filter_logs(raw)

    except FileNotFoundError:
        return (
            "[ERROR] Không tìm thấy lệnh 'adb'. "
            "Hãy cài ADB: https://developer.android.com/tools/releases/platform-tools"
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] ADB timeout — device có thể đang bận."
    except Exception as e:
        return f"[ERROR] {e}"


def _get_recent_logs_raw(device_serial: str, lines: int = 200) -> str:
    """Lấy log gần đây — dùng nội bộ cho watch mode (không pre-filter)."""
    try:
        cmd = ["adb", "-s", device_serial, "logcat", "-d", "-t", str(lines), "*:V"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return result.stdout
    except Exception:
        return ""


def list_adb_devices() -> str:
    try:
        result = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=10
        )
        return result.stdout or "Không có thiết bị nào."
    except FileNotFoundError:
        return "[ERROR] ADB không được cài đặt."
    except Exception as e:
        return f"[ERROR] {e}"


def get_all_connected_devices() -> list[str]:
    """Trả về danh sách serial của tất cả device đang kết nối."""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        )
        return [
            line.split()[0]
            for line in result.stdout.splitlines()[1:]
            if line.strip() and "device" in line and "offline" not in line
        ]
    except Exception:
        return []


def get_device_info(device_serial: str) -> str:
    info = {}

    try:
        r = subprocess.run(
            ["adb", "-s", device_serial, "shell", "settings", "get", "global", "device_name"],
            capture_output=True, text=True, timeout=5,
        )
        name = r.stdout.strip()
        if name and name != "null":
            info["device_name"] = name
    except Exception:
        pass

    props = {
        "ro.product.model": "model",
        "ro.build.version.release": "android_version",
    }
    for prop, key in props.items():
        try:
            r = subprocess.run(
                ["adb", "-s", device_serial, "shell", "getprop", prop],
                capture_output=True, text=True, timeout=5,
            )
            info[key] = r.stdout.strip()
        except Exception:
            pass

    info["serial"] = device_serial
    return json.dumps(info, ensure_ascii=False)


def execute_tool(name: str, tool_input: dict) -> str:
    if name == "read_log_file":
        return read_log_file(tool_input["file_path"], tool_input.get("tail_lines"))
    if name == "get_adb_logs":
        return get_adb_logs(
            tag_filter=tool_input.get("tag_filter", ""),
            lines=tool_input.get("lines", 1000),
            device_serial=tool_input.get("device_serial", ""),
            priority=tool_input.get("priority", "V"),
        )
    if name == "list_adb_devices":
        return list_adb_devices()
    if name == "get_device_info":
        return get_device_info(tool_input["device_serial"])
    return f"[ERROR] Tool không tồn tại: {name}"

# ─── Screenshot Capture ───────────────────────────────────────────────────────

def capture_screenshot(device_serial: str, output_dir: str = "sessions") -> Optional[str]:
    """
    Chụp màn hình device qua ADB và lưu vào output_dir.

    Returns:
        Đường dẫn file screenshot, hoặc None nếu thất bại.
    """
    try:
        sessions_dir = Path(output_dir)
        sessions_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_serial = device_serial.replace(":", "-").replace(".", "-")
        remote_path = "/sdcard/cc_bug_screenshot.png"
        local_path = sessions_dir / f"{ts}_{safe_serial}_screenshot.png"

        # Chụp trên device
        subprocess.run(
            ["adb", "-s", device_serial, "shell", "screencap", "-p", remote_path],
            capture_output=True, timeout=10, check=True,
        )

        # Pull về máy
        subprocess.run(
            ["adb", "-s", device_serial, "pull", remote_path, str(local_path)],
            capture_output=True, timeout=15, check=True,
        )

        # Xóa file tạm trên device
        subprocess.run(
            ["adb", "-s", device_serial, "shell", "rm", remote_path],
            capture_output=True, timeout=5,
        )

        print(f"  [Screenshot] Đã lưu: {local_path}")
        return str(local_path)

    except subprocess.CalledProcessError as e:
        print(f"  [Screenshot] Lỗi ADB: {e}")
        return None
    except Exception as e:
        print(f"  [Screenshot] Lỗi: {e}")
        return None


# ─── Session Saving ───────────────────────────────────────────────────────────

def save_session(
    logs: str,
    report: dict,
    device_serial: str = "",
    output_dir: str = "sessions",
) -> str:
    """
    Lưu log thô và bug report vào thư mục sessions/.

    Returns:
        Prefix path của session (không có extension).
    """
    sessions_dir = Path(output_dir)
    sessions_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_serial = (
        device_serial.replace(":", "-").replace(".", "-") if device_serial else "file"
    )
    prefix = f"{ts}_{safe_serial}"

    log_file = sessions_dir / f"{prefix}_log.txt"
    log_file.write_text(logs, encoding="utf-8")

    report_file = sessions_dir / f"{prefix}_report.json"
    report_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"  [Session] Đã lưu: {sessions_dir}/{prefix}_{{log,report}}")
    return str(sessions_dir / prefix)


# ─── Agent Main Function ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """Bạn là chuyên gia phân tích bug cho game Cocos (Cocos2d-x / Cocos Creator) chạy trên LDPlayer.

Nhiệm vụ của bạn:
1. Đọc và phân tích log từ LDPlayer (qua ADB hoặc file)
2. Xác định các bug, lỗi crash, và hành vi bất thường
3. Tái hiện chuỗi sự kiện dẫn đến bug
4. Tạo bug report có cấu trúc rõ ràng

Khi phân tích log:
- Tìm các pattern: ERROR, FATAL, CRASH, Exception, Assertion failed, Stack trace
- Theo dõi Cocos-specific errors: Director, SceneManager, Scheduler, EventDispatcher, JSB errors
- Ghi nhận timestamp và thứ tự sự kiện
- Phân biệt root cause vs triệu chứng
- Theo dõi memory warnings, OpenGL errors, network failures

Kết quả trả về PHẢI là một JSON hợp lệ với cấu trúc sau:
{
  "title": "Tên bug ngắn gọn",
  "summary": "Mô tả tổng quan bug",
  "severity": "Critical|High|Medium|Low",
  "environment": {
    "device_name": "Tên device do QA đặt, ví dụ: Device-1",
    "device_model": "...",
    "android_version": "...",
    "game_version": "..."
  },
  "root_cause": "Nguyên nhân gốc rễ",
  "reproduction_steps": [
    "Bước 1: ...",
    "Bước 2: ...",
    "Bước 3: ..."
  ],
  "expected_behavior": "Hành vi mong đợi",
  "actual_behavior": "Hành vi thực tế",
  "evidence": [
    {
      "description": "Mô tả đoạn log",
      "log_snippet": "Đoạn log liên quan"
    }
  ],
  "additional_notes": "Ghi chú thêm (nếu có)"
}

Nếu không tìm thấy bug, hãy trả về JSON với title="No bugs found" và giải thích trong summary."""


def analyze_logs(
    source: str,
    extra_context: str = "",
    device_serial: str = "",
    save_logs: bool = False,
) -> dict:
    """
    Phân tích log từ 1 device và trả về bug report.

    Args:
        source: "adb" hoặc đường dẫn file log
        extra_context: Thông tin thêm từ QA
        device_serial: Serial ADB cụ thể (khi chạy multi-device)
        save_logs: Tự động lưu log và report vào sessions/
    """
    if source.lower() == "adb":
        if device_serial:
            user_msg = (
                f"Hãy lấy thông tin device và log từ thiết bị ADB có serial: {device_serial}, "
                "sau đó phân tích để tìm bug."
            )
        else:
            user_msg = (
                "Hãy kiểm tra các thiết bị ADB đang kết nối, "
                "sau đó lấy log từ LDPlayer và phân tích để tìm bug."
            )
    else:
        user_msg = f"Hãy đọc và phân tích file log tại: {source}"

    if extra_context:
        user_msg += f"\n\nThông tin thêm từ QA: {extra_context}"

    user_msg += "\n\nCuối cùng, hãy trả về kết quả phân tích dưới dạng JSON theo cấu trúc đã mô tả."

    messages = [{"role": "user", "content": user_msg}]
    label = device_serial or source
    print(f"[Log Analyzer] Đang phân tích: {label}")

    last_logs = ""  # Lưu log thô để save session

    while True:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        for block in response.content:
            if block.type == "tool_use":
                print(f"  [{label}] → {block.name}({json.dumps(block.input, ensure_ascii=False)})")

        if response.stop_reason == "end_turn":
            final_text = next(
                (b.text for b in response.content if b.type == "text"), ""
            )
            report = _parse_bug_report(final_text)
            if device_serial:
                report["_device_serial"] = device_serial

            if save_logs and last_logs:
                save_session(last_logs, report, device_serial)

            return report

        if response.stop_reason != "tool_use":
            return {"error": f"Unexpected stop_reason: {response.stop_reason}"}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                # Lưu log để save session sau
                if block.name in ("get_adb_logs", "read_log_file"):
                    last_logs = result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})


def analyze_all_devices(extra_context: str = "", save_logs: bool = False) -> list[dict]:
    """
    Phân tích log từ TẤT CẢ device đang kết nối — chạy song song.

    Returns:
        Danh sách bug report, mỗi report tương ứng 1 device.
    """
    devices = get_all_connected_devices()
    if not devices:
        print("[Log Analyzer] Không tìm thấy device nào.")
        return []

    print(f"[Log Analyzer] Tìm thấy {len(devices)} device: {', '.join(devices)}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(devices)) as executor:
        futures = {
            executor.submit(analyze_logs, "adb", extra_context, serial, save_logs): serial
            for serial in devices
        }
        results = []
        for future in concurrent.futures.as_completed(futures):
            serial = futures[future]
            try:
                report = future.result()
                results.append(report)
                print(f"  ✓ Xong: {serial}")
            except Exception as e:
                results.append({"error": str(e), "_device_serial": serial})
                print(f"  ✗ Lỗi: {serial} — {e}")

    return results


# ─── Watch Mode ───────────────────────────────────────────────────────────────

def watch_logs(
    interval: int = 30,
    device_serial: str = "",
    extra_context: str = "",
    on_bug_found: Optional[Callable] = None,
    save_logs: bool = False,
    cooldown: int = 120,
):
    """
    Theo dõi log liên tục, tự động phát hiện và báo cáo bug.

    Args:
        interval: Khoảng thời gian kiểm tra log (giây)
        device_serial: Serial ADB của device cần theo dõi
        extra_context: Thông tin thêm từ QA
        on_bug_found: Callback(report, screenshot_path) khi phát hiện bug
        save_logs: Tự động lưu log và report
        cooldown: Thời gian chờ (giây) sau khi báo cáo bug trước khi check lại
    """
    # Phát hiện device nếu chưa có serial
    if not device_serial:
        devices = get_all_connected_devices()
        if not devices:
            print("[Watch] Không tìm thấy device ADB. Kiểm tra LDPlayer và ADB.")
            return
        # Ưu tiên LDPlayer
        device_serial = next(
            (d for d in devices if "5554" in d or "127.0.0.1" in d or "localhost" in d),
            devices[0],
        )

    print(f"[Watch] Theo dõi device: {device_serial}")
    print(f"[Watch] Kiểm tra mỗi {interval}s | Cooldown sau bug: {cooldown}s")
    print("[Watch] Nhấn Ctrl+C để dừng.\n")

    last_bug_time = 0.0

    try:
        tick = 0
        while True:
            tick += 1
            now = time.time()

            # Trong cooldown — bỏ qua
            if now - last_bug_time < cooldown:
                remaining = int(cooldown - (now - last_bug_time))
                print(f"\r[Watch] Cooldown còn {remaining}s...   ", end="", flush=True)
                time.sleep(interval)
                continue

            # Quick check: lấy 200 dòng log gần nhất, không cần AI
            print(f"\r[Watch] #{tick} Checking {datetime.now().strftime('%H:%M:%S')}...", end="", flush=True)
            recent_logs = _get_recent_logs_raw(device_serial, lines=200)

            if not recent_logs:
                time.sleep(interval)
                continue

            if not has_critical_errors(recent_logs):
                time.sleep(interval)
                continue

            # Có lỗi → phân tích đầy đủ bằng Claude
            print(f"\n[Watch] ⚠️  Phát hiện lỗi! Đang phân tích sâu...")

            # Chụp screenshot trước khi game crash hoàn toàn
            screenshot_path = capture_screenshot(device_serial)

            report = analyze_logs(
                "adb",
                extra_context=extra_context,
                device_serial=device_serial,
                save_logs=save_logs,
            )

            last_bug_time = time.time()

            if on_bug_found:
                on_bug_found(report, screenshot_path)
            else:
                title = report.get("title", "Unknown")
                severity = report.get("severity", "?")
                print(f"[Watch] Bug: [{severity}] {title}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\n[Watch] Dừng theo dõi.")


# ─── JSON Parsing ─────────────────────────────────────────────────────────────

def _parse_bug_report(text: str) -> dict:
    """
    Trích xuất JSON từ response text.
    Dùng brace-counting thay vì regex để xử lý JSON lồng nhau đúng cách.
    """
    # Method 1: JSON trong markdown code fence
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Method 2: Brace counting — tìm JSON object đầu tiên hoàn chỉnh
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    # Fallback
    return {"title": "Analysis Result", "summary": text, "raw": True}
