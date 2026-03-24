"""
Agent 1: Cocos Log Analyzer
Đọc log từ LDPlayer (qua ADB hoặc file), phân tích và tạo bug reproduction flow.
Hỗ trợ nhiều device cùng lúc.
"""

import subprocess
import json
import concurrent.futures
from pathlib import Path
from typing import Optional
import anthropic

client = anthropic.Anthropic()

# ─── Tool Definitions ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_log_file",
        "description": (
            "Đọc nội dung file log từ đĩa. Dùng khi log đã được lưu ra file."
        ),
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
                    "description": (
                        "Lọc theo logcat tag, ví dụ: 'cocos2d' hoặc 'jsb'. "
                        "Bỏ trống để lấy tất cả log."
                    ),
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
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
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
        return f"[FILE: {path.name} | {size_kb:.1f} KB | {len(lines)} dòng]\n\n{content}"
    except Exception as e:
        return f"[ERROR] Không đọc được file: {e}"


def get_adb_logs(
    tag_filter: str = "",
    lines: int = 1000,
    device_serial: str = "",
    priority: str = "V",
) -> str:
    try:
        # Tìm device nếu không chỉ định
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
            # Ưu tiên LDPlayer (thường là localhost:5554 hoặc 127.0.0.1:5xxx)
            ldplayer = next(
                (d for d in device_lines if "5554" in d or "127.0.0.1" in d or "localhost" in d),
                device_lines[0],
            )
            device_serial = ldplayer

        # Build lệnh logcat
        cmd = ["adb", "-s", device_serial, "logcat", "-d", "-t", str(lines)]

        if tag_filter:
            # Lọc theo tag, hiển thị từ priority chỉ định
            cmd += [f"{tag_filter}:{priority}", "*:S"]
        else:
            cmd += [f"*:{priority}"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            return f"[ERROR] ADB thất bại: {result.stderr}"

        output = result.stdout.strip()
        if not output:
            return f"[INFO] Không có log từ device {device_serial} với filter hiện tại."

        return f"[ADB LOGS | device={device_serial} | filter={tag_filter or 'all'} | priority>={priority}]\n\n{output}"

    except FileNotFoundError:
        return (
            "[ERROR] Không tìm thấy lệnh 'adb'. "
            "Hãy cài ADB: https://developer.android.com/tools/releases/platform-tools"
        )
    except subprocess.TimeoutExpired:
        return "[ERROR] ADB timeout — device có thể đang bận."
    except Exception as e:
        return f"[ERROR] {e}"


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

    # Lấy tên device do user đặt thủ công (ưu tiên nhất)
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

    # Các thông tin phụ
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


def analyze_logs(source: str, extra_context: str = "", device_serial: str = "") -> dict:
    """
    Phân tích log từ 1 device và trả về bug report.

    Args:
        source: "adb" hoặc đường dẫn file log
        extra_context: Thông tin thêm từ QA
        device_serial: Serial ADB cụ thể (khi chạy multi-device)
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
            # Gắn device_serial vào report để phân biệt
            if device_serial:
                report["_device_serial"] = device_serial
            return report

        if response.stop_reason != "tool_use":
            return {"error": f"Unexpected stop_reason: {response.stop_reason}"}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})


def analyze_all_devices(extra_context: str = "") -> list[dict]:
    """
    Phân tích log từ TẤT CẢ device đang kết nối — chạy song song.

    Returns:
        Danh sách bug report, mỗi report tương ứng 1 device
    """
    devices = get_all_connected_devices()
    if not devices:
        print("[Log Analyzer] Không tìm thấy device nào.")
        return []

    print(f"[Log Analyzer] Tìm thấy {len(devices)} device: {', '.join(devices)}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(devices)) as executor:
        futures = {
            executor.submit(analyze_logs, "adb", extra_context, serial): serial
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


def _parse_bug_report(text: str) -> dict:
    """Trích xuất JSON từ response text."""
    import re

    # Tìm JSON block trong markdown code fence
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        # Tìm JSON trực tiếp trong text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        json_str = match.group(0) if match else None

    if json_str:
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # Fallback: trả về raw text
    return {"title": "Analysis Result", "summary": text, "raw": True}
