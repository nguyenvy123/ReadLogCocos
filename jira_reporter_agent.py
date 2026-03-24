"""
Agent 2: Jira Bug Reporter
Nhận bug report từ Log Analyzer và tạo Jira ticket tự động.
"""

import json
import os
import re
from typing import Optional
import requests
import anthropic
from requests.auth import HTTPBasicAuth

client = anthropic.Anthropic()

# ─── Jira API Helper ──────────────────────────────────────────────────────────

class JiraClient:
    def __init__(self):
        self.base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        self.email = os.getenv("JIRA_EMAIL", "")
        self.token = os.getenv("JIRA_API_TOKEN", "")
        self.project_key = os.getenv("JIRA_PROJECT_KEY", "GAME")
        self.auth = HTTPBasicAuth(self.email, self.token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/rest/api/3/{path.lstrip('/')}"

    def get_issue_types(self) -> list:
        r = requests.get(
            self._url(f"project/{self.project_key}"),
            auth=self.auth, headers=self.headers, timeout=10
        )
        if r.ok:
            return [it["name"] for it in r.json().get("issueTypes", [])]
        return ["Bug", "Task", "Story"]

    def get_priorities(self) -> list:
        r = requests.get(
            self._url("priority"), auth=self.auth, headers=self.headers, timeout=10
        )
        if r.ok:
            return [p["name"] for p in r.json()]
        return ["Highest", "High", "Medium", "Low", "Lowest"]

    def create_issue(self, payload: dict) -> dict:
        r = requests.post(
            self._url("issue"),
            auth=self.auth,
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        if r.ok:
            data = r.json()
            issue_url = f"{self.base_url}/browse/{data['key']}"
            return {"success": True, "key": data["key"], "url": issue_url, "id": data["id"]}
        return {"success": False, "error": r.text, "status_code": r.status_code}

    def add_comment(self, issue_key: str, comment_body: dict) -> dict:
        r = requests.post(
            self._url(f"issue/{issue_key}/comment"),
            auth=self.auth,
            headers=self.headers,
            json={"body": comment_body},
            timeout=10,
        )
        return {"success": r.ok, "status_code": r.status_code}

    def get_project_info(self) -> str:
        r = requests.get(
            self._url(f"project/{self.project_key}"),
            auth=self.auth, headers=self.headers, timeout=10
        )
        if r.ok:
            d = r.json()
            return json.dumps({
                "key": d.get("key"),
                "name": d.get("name"),
                "issue_types": [it["name"] for it in d.get("issueTypes", [])],
            }, ensure_ascii=False)
        return f"Lỗi kết nối Jira: {r.status_code} — {r.text}"


jira = JiraClient()

# ─── Tool Definitions ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_jira_project_info",
        "description": "Lấy thông tin project Jira (issue types, priorities, fields).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_jira_bug",
        "description": "Tạo bug ticket trên Jira từ thông tin bug report.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Tiêu đề ngắn của bug"},
                "description_adf": {
                    "type": "object",
                    "description": "Mô tả bug theo định dạng Atlassian Document Format (ADF)",
                },
                "priority": {
                    "type": "string",
                    "enum": ["Highest", "High", "Medium", "Low", "Lowest"],
                    "description": "Độ ưu tiên của bug",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Nhãn cho ticket, ví dụ: ['cocos', 'crash', 'ldplayer']",
                },
                "custom_fields": {
                    "type": "object",
                    "description": "Các custom fields của Jira project (nếu có)",
                },
            },
            "required": ["summary", "description_adf", "priority"],
        },
    },
    {
        "name": "add_jira_comment",
        "description": "Thêm comment vào Jira ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Key của issue, ví dụ: GAME-123"},
                "comment_text": {"type": "string", "description": "Nội dung comment"},
            },
            "required": ["issue_key", "comment_text"],
        },
    },
]

# ─── Tool Implementations ─────────────────────────────────────────────────────

def tool_get_jira_project_info() -> str:
    return jira.get_project_info()


def tool_create_jira_bug(
    summary: str,
    description_adf: dict,
    priority: str,
    labels: list = None,
    custom_fields: dict = None,
) -> str:
    payload = {
        "fields": {
            "project": {"key": jira.project_key},
            "issuetype": {"name": "Bug"},
            "summary": summary,
            "description": description_adf,
            "priority": {"name": priority},
            "labels": labels or [],
        }
    }
    if custom_fields:
        payload["fields"].update(custom_fields)

    result = jira.create_issue(payload)
    return json.dumps(result, ensure_ascii=False)


def tool_add_jira_comment(issue_key: str, comment_text: str) -> str:
    body = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": comment_text}],
            }
        ],
    }
    result = jira.add_comment(issue_key, body)
    return json.dumps(result, ensure_ascii=False)


def execute_tool(name: str, tool_input: dict) -> str:
    if name == "get_jira_project_info":
        return tool_get_jira_project_info()
    if name == "create_jira_bug":
        return tool_create_jira_bug(
            summary=tool_input["summary"],
            description_adf=tool_input["description_adf"],
            priority=tool_input["priority"],
            labels=tool_input.get("labels", []),
            custom_fields=tool_input.get("custom_fields"),
        )
    if name == "add_jira_comment":
        return tool_add_jira_comment(
            tool_input["issue_key"], tool_input["comment_text"]
        )
    return f"[ERROR] Tool không tồn tại: {name}"


# ─── Agent Main Function ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """Bạn là chuyên gia tạo bug ticket trên Jira cho team game Cocos.

Nhiệm vụ:
1. Nhận bug report có cấu trúc từ Log Analyzer Agent
2. Kiểm tra thông tin project Jira
3. Tạo Jira ticket chuyên nghiệp với đầy đủ thông tin

Quy tắc tạo ticket:
- Summary: ngắn gọn, rõ ràng, dưới 100 ký tự
- Description dùng ADF (Atlassian Document Format) với các section:
  * 🐛 Mô tả bug
  * 📋 Reproduction Steps (numbered list)
  * ✅ Expected Behavior
  * ❌ Actual Behavior
  * 🔍 Root Cause
  * 📊 Log Evidence (code block)
  * 📱 Environment
- Priority mapping: Critical→Highest, High→High, Medium→Medium, Low→Low
- Labels: luôn thêm 'cocos', 'ldplayer', và labels phù hợp với loại bug
- Sau khi tạo xong, in rõ URL của ticket

ADF Format example cho description:
{
  "type": "doc",
  "version": 1,
  "content": [
    {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "🐛 Mô tả"}]},
    {"type": "paragraph", "content": [{"type": "text", "text": "Nội dung..."}]},
    {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "📋 Reproduction Steps"}]},
    {"type": "orderedList", "content": [
      {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Bước 1"}]}]}
    ]},
    {"type": "codeBlock", "attrs": {"language": "text"}, "content": [{"type": "text", "text": "log snippet..."}]}
  ]
}"""


def _build_reporter_prompt(bug_report: dict) -> str:
    report_json = json.dumps(bug_report, ensure_ascii=False, indent=2)
    return (
        f"Đây là bug report từ Log Analyzer Agent:\n\n```json\n{report_json}\n```\n\n"
        "Hãy kiểm tra project Jira và tạo bug ticket phù hợp cho bug này."
    )


def _build_multi_device_prompt(reports: list[dict]) -> str:
    """Tạo prompt cho nhiều device — gộp thành 1 ticket, chỉ ghi device nào bị bug."""
    reports_json = json.dumps(reports, ensure_ascii=False, indent=2)
    return (
        f"Đây là bug report từ {len(reports)} device khác nhau:\n\n```json\n{reports_json}\n```\n\n"
        "Hãy:\n"
        "1. Xác định bug nào là chung (xuất hiện nhiều device) vs bug riêng từng device\n"
        "2. Với mỗi bug: tạo 1 ticket Jira, trong description thêm dòng "
        "'Affected Devices: Device-1, Device-2' — dùng device_name trong environment (do QA đặt), "
        "nếu không có thì dùng serial. Không cần info chi tiết khác.\n"
        "3. Kiểm tra project Jira trước khi tạo ticket."
    )


def report_bugs_multi_device(reports: list[dict]) -> list[dict]:
    """
    Nhận danh sách bug report từ nhiều device, tạo Jira tickets.
    Bug chung nhiều device → 1 ticket. Bug riêng 1 device → ticket riêng.
    """
    if not reports:
        return []

    messages = [{"role": "user", "content": _build_multi_device_prompt(reports)}]
    print(f"[Jira Reporter] Đang xử lý {len(reports)} device...")

    created_issues = []

    while True:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        for block in response.content:
            if block.type == "tool_use":
                print(f"  → Tool: {block.name}")

        if response.stop_reason == "end_turn":
            return created_issues or [{"message": next(
                (b.text for b in response.content if b.type == "text"), ""
            )}]

        if response.stop_reason != "tool_use":
            return [{"error": f"Unexpected stop_reason: {response.stop_reason}"}]

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                result_str = execute_tool(block.name, block.input)
                if block.name == "create_jira_bug":
                    try:
                        parsed = json.loads(result_str)
                        if parsed.get("success"):
                            created_issues.append(parsed)
                    except Exception:
                        pass
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        messages.append({"role": "user", "content": tool_results})


def report_bug_to_jira(bug_report: dict) -> dict:
    """
    Tạo Jira ticket từ bug report.

    Args:
        bug_report: dict từ log_analyzer_agent.analyze_logs()

    Returns:
        dict với thông tin ticket đã tạo: {"key": "GAME-123", "url": "..."}
    """
    if bug_report.get("title") == "No bugs found":
        return {"skipped": True, "reason": "No bugs found in logs"}

    messages = [{"role": "user", "content": _build_reporter_prompt(bug_report)}]

    print("[Jira Reporter] Đang tạo Jira ticket...")

    created_issue = None

    while True:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        for block in response.content:
            if block.type == "tool_use":
                print(f"  → Tool: {block.name}")

        if response.stop_reason == "end_turn":
            final_text = next(
                (b.text for b in response.content if b.type == "text"), ""
            )
            result = created_issue or {"message": final_text}
            return result

        if response.stop_reason != "tool_use":
            return {"error": f"Unexpected stop_reason: {response.stop_reason}"}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                result_str = execute_tool(block.name, block.input)

                # Capture kết quả create_jira_bug
                if block.name == "create_jira_bug":
                    try:
                        parsed = json.loads(result_str)
                        if parsed.get("success"):
                            created_issue = parsed
                    except Exception:
                        pass

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        messages.append({"role": "user", "content": tool_results})
