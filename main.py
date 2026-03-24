"""
Main Orchestrator
Kết hợp Log Analyzer Agent và Jira Reporter Agent.

Usage:
  python main.py --source adb
  python main.py --source path/to/game.log
  python main.py --source adb --context "Bug xảy ra sau khi click nút Shop"
  python main.py --source adb --no-jira   # Chỉ phân tích, không tạo Jira ticket
"""

import argparse
import json
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def check_env():
    """Kiểm tra các biến môi trường cần thiết."""
    missing = []
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    return missing


def print_bug_report(report: dict):
    print("\n" + "=" * 60)
    print("📋 BUG REPORT")
    print("=" * 60)

    if report.get("raw"):
        print(report.get("summary", ""))
        return

    if report.get("error"):
        print(f"❌ Lỗi: {report['error']}")
        return

    severity_icon = {
        "Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🟢"
    }.get(report.get("severity", ""), "⚪")

    print(f"\n🐛 Title     : {report.get('title', 'N/A')}")
    print(f"{severity_icon} Severity   : {report.get('severity', 'N/A')}")
    print(f"\n📝 Summary   :\n   {report.get('summary', 'N/A')}")
    print(f"\n🔍 Root Cause:\n   {report.get('root_cause', 'N/A')}")

    steps = report.get("reproduction_steps", [])
    if steps:
        print("\n📋 Reproduction Steps:")
        for i, step in enumerate(steps, 1):
            print(f"   {i}. {step}")

    print(f"\n✅ Expected  : {report.get('expected_behavior', 'N/A')}")
    print(f"❌ Actual    : {report.get('actual_behavior', 'N/A')}")

    evidence = report.get("evidence", [])
    if evidence:
        print("\n🔎 Evidence:")
        for ev in evidence[:3]:  # Giới hạn 3 evidence
            print(f"   [{ev.get('description', '')}]")
            snippet = ev.get("log_snippet", "")
            if snippet:
                # In tối đa 5 dòng mỗi snippet
                lines = snippet.strip().splitlines()[:5]
                for line in lines:
                    print(f"   | {line}")

    env = report.get("environment", {})
    if env:
        print(f"\n📱 Environment: {json.dumps(env, ensure_ascii=False)}")

    if report.get("additional_notes"):
        print(f"\n📌 Notes: {report['additional_notes']}")


def print_jira_result(result: dict):
    print("\n" + "=" * 60)
    print("🎫 JIRA TICKET")
    print("=" * 60)

    if result.get("skipped"):
        print(f"⏭️  Bỏ qua: {result.get('reason')}")
        return

    if result.get("error"):
        print(f"❌ Lỗi tạo ticket: {result['error']}")
        return

    if result.get("success"):
        print(f"\n✅ Tạo ticket thành công!")
        print(f"   🔑 Key : {result.get('key')}")
        print(f"   🔗 URL : {result.get('url')}")
    elif result.get("message"):
        print(f"\n📄 {result['message']}")
    else:
        print(f"\n⚠️  Kết quả không rõ ràng: {json.dumps(result, ensure_ascii=False)}")


def main():
    parser = argparse.ArgumentParser(
        description="Cocos Log Bug Analyzer + Jira Reporter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ:
  python main.py --source adb
  python main.py --source C:/logs/game.log
  python main.py --source adb --context "Crash sau 30 giây gameplay"
  python main.py --source adb --no-jira
        """,
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Nguồn log: 'adb' để lấy từ LDPlayer, hoặc đường dẫn file log",
    )
    parser.add_argument(
        "--context",
        default="",
        help="Thông tin thêm về bug để hỗ trợ phân tích",
    )
    parser.add_argument(
        "--no-jira",
        action="store_true",
        help="Chỉ phân tích log, không tạo Jira ticket",
    )
    parser.add_argument(
        "--output",
        help="Lưu bug report ra file JSON",
    )

    args = parser.parse_args()

    # Kiểm tra env
    missing = check_env()
    if missing:
        print(f"❌ Thiếu biến môi trường: {', '.join(missing)}")
        print("   Tạo file .env từ .env.example và điền thông tin vào.")
        sys.exit(1)

    if not args.no_jira:
        jira_missing = [
            v for v in ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
            if not os.getenv(v)
        ]
        if jira_missing:
            print(f"⚠️  Thiếu config Jira: {', '.join(jira_missing)}")
            print("   Dùng --no-jira để chỉ phân tích log.")
            sys.exit(1)

    print(f"\n🚀 Bắt đầu phân tích log từ: {args.source}")
    if args.context:
        print(f"   Context: {args.context}")

    # ── Agent 1: Phân tích log ──────────────────────────────────────────────
    multi_device = args.source.lower() == "adb:all"

    if multi_device:
        from log_analyzer_agent import analyze_all_devices
        bug_reports = analyze_all_devices(args.context)
        for r in bug_reports:
            print_bug_report(r)
        all_reports = bug_reports
    else:
        from log_analyzer_agent import analyze_logs
        bug_report = analyze_logs(args.source, args.context)
        print_bug_report(bug_report)
        all_reports = [bug_report]

    # Lưu ra file nếu cần
    if args.output:
        data = all_reports if multi_device else all_reports[0]
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n💾 Bug report đã lưu: {args.output}")

    # ── Agent 2: Report lên Jira ────────────────────────────────────────────
    if not args.no_jira:
        if multi_device:
            from jira_reporter_agent import report_bugs_multi_device
            jira_results = report_bugs_multi_device(all_reports)
            print("\n" + "=" * 60)
            print("🎫 JIRA TICKETS")
            print("=" * 60)
            for r in jira_results:
                print_jira_result(r)
        else:
            from jira_reporter_agent import report_bug_to_jira
            jira_result = report_bug_to_jira(all_reports[0])
            print_jira_result(jira_result)

    print("\n✅ Hoàn thành!")


if __name__ == "__main__":
    main()
