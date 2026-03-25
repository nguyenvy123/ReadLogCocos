"""
Main Orchestrator
Kết hợp Log Analyzer Agent và Jira Reporter Agent.

Usage:
  python main.py --source adb
  python main.py --source path/to/game.log
  python main.py --source adb --context "Bug xảy ra sau khi click nút Shop"
  python main.py --source adb --no-jira           # Chỉ phân tích, không tạo Jira ticket
  python main.py --source adb --watch             # Watch mode: theo dõi liên tục
  python main.py --source adb --watch --interval 60 --save-logs
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
    print("BUG REPORT")
    print("=" * 60)

    if report.get("raw"):
        print(report.get("summary", ""))
        return

    if report.get("error"):
        print(f"Loi: {report['error']}")
        return

    severity_icon = {
        "Critical": "[CRITICAL]", "High": "[HIGH]", "Medium": "[MEDIUM]", "Low": "[LOW]"
    }.get(report.get("severity", ""), "[?]")

    print(f"\nTitle     : {report.get('title', 'N/A')}")
    print(f"Severity   : {severity_icon} {report.get('severity', 'N/A')}")
    print(f"\nSummary   :\n   {report.get('summary', 'N/A')}")
    print(f"\nRoot Cause:\n   {report.get('root_cause', 'N/A')}")

    steps = report.get("reproduction_steps", [])
    if steps:
        print("\nReproduction Steps:")
        for i, step in enumerate(steps, 1):
            print(f"   {i}. {step}")

    print(f"\nExpected  : {report.get('expected_behavior', 'N/A')}")
    print(f"Actual    : {report.get('actual_behavior', 'N/A')}")

    evidence = report.get("evidence", [])
    if evidence:
        print("\nEvidence:")
        for ev in evidence[:3]:
            print(f"   [{ev.get('description', '')}]")
            snippet = ev.get("log_snippet", "")
            if snippet:
                for line in snippet.strip().splitlines()[:5]:
                    print(f"   | {line}")

    env = report.get("environment", {})
    if env:
        print(f"\nEnvironment: {json.dumps(env, ensure_ascii=False)}")

    if report.get("additional_notes"):
        print(f"\nNotes: {report['additional_notes']}")


def print_jira_result(result: dict):
    print("\n" + "=" * 60)
    print("JIRA TICKET")
    print("=" * 60)

    if result.get("skipped"):
        print(f"Bo qua: {result.get('reason')}")
        return

    if result.get("error"):
        print(f"Loi tao ticket: {result['error']}")
        return

    if result.get("success"):
        action = result.get("action", "")
        if action == "commented_on_existing":
            print(f"\nBug duplicate — da them comment vao ticket cu!")
        else:
            print(f"\nTao ticket thanh cong!")
        print(f"   Key : {result.get('key')}")
        print(f"   URL : {result.get('url')}")
    elif result.get("message"):
        print(f"\n{result['message']}")
    else:
        print(f"\nKet qua: {json.dumps(result, ensure_ascii=False)}")


def main():
    parser = argparse.ArgumentParser(
        description="Cocos Log Bug Analyzer + Jira Reporter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Vi du:
  python main.py --source adb
  python main.py --source C:/logs/game.log
  python main.py --source adb --context "Crash sau 30 giay gameplay"
  python main.py --source adb --no-jira
  python main.py --source adb --watch
  python main.py --source adb --watch --interval 60 --save-logs
        """,
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Nguon log: 'adb' de lay tu LDPlayer, hoac duong dan file log",
    )
    parser.add_argument(
        "--context",
        default="",
        help="Thong tin them ve bug de ho tro phan tich",
    )
    parser.add_argument(
        "--no-jira",
        action="store_true",
        help="Chi phan tich log, khong tao Jira ticket",
    )
    parser.add_argument(
        "--output",
        help="Luu bug report ra file JSON",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode: theo doi log lien tuc, tu dong phat hien bug",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Khoang thoi gian kiem tra log trong watch mode (giay, mac dinh 30)",
    )
    parser.add_argument(
        "--save-logs",
        action="store_true",
        help="Tu dong luu log va report vao thu muc sessions/",
    )

    args = parser.parse_args()

    # Kiểm tra env
    missing = check_env()
    if missing:
        print(f"Thieu bien moi truong: {', '.join(missing)}")
        print("   Tao file .env tu .env.example va dien thong tin vao.")
        sys.exit(1)

    if not args.no_jira:
        jira_missing = [
            v for v in ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
            if not os.getenv(v)
        ]
        if jira_missing:
            print(f"Thieu config Jira: {', '.join(jira_missing)}")
            print("   Dung --no-jira de chi phan tich log.")
            sys.exit(1)

    print(f"\nBat dau phan tich log tu: {args.source}")
    if args.context:
        print(f"   Context: {args.context}")

    # ── Watch Mode ──────────────────────────────────────────────────────────────
    if args.watch:
        if args.source.lower() not in ("adb", "adb:all"):
            print("Watch mode chi ho tro source 'adb'. Dung --source adb --watch.")
            sys.exit(1)

        from log_analyzer_agent import watch_logs
        from jira_reporter_agent import report_bug_to_jira

        all_reports = []

        def on_bug_found(report: dict, screenshot_path=None):
            print_bug_report(report)
            all_reports.append(report)

            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(all_reports, f, ensure_ascii=False, indent=2)
                print(f"\nBug report da luu: {args.output}")

            if not args.no_jira:
                from jira_reporter_agent import report_bug_to_jira
                jira_result = report_bug_to_jira(report, screenshot_path)
                print_jira_result(jira_result)

        watch_logs(
            interval=args.interval,
            extra_context=args.context,
            on_bug_found=on_bug_found,
            save_logs=args.save_logs,
        )
        print("\nHoan thanh!")
        return

    # ── Single / Multi Device Analysis ──────────────────────────────────────────
    multi_device = args.source.lower() == "adb:all"

    if multi_device:
        from log_analyzer_agent import analyze_all_devices, capture_screenshot, get_all_connected_devices
        bug_reports = analyze_all_devices(args.context, save_logs=args.save_logs)
        for r in bug_reports:
            print_bug_report(r)
        all_reports = bug_reports

        # Capture screenshots cho tất cả device
        screenshot_paths = {}
        if not args.no_jira:
            for serial in get_all_connected_devices():
                shot = capture_screenshot(serial)
                if shot:
                    screenshot_paths[serial] = shot
    else:
        from log_analyzer_agent import analyze_logs, capture_screenshot
        bug_report = analyze_logs(args.source, args.context, save_logs=args.save_logs)
        print_bug_report(bug_report)
        all_reports = [bug_report]
        screenshot_paths = {}

        # Capture screenshot nếu là ADB
        if args.source.lower() == "adb" and not args.no_jira:
            from log_analyzer_agent import get_all_connected_devices
            devices = get_all_connected_devices()
            if devices:
                device = next(
                    (d for d in devices if "5554" in d or "127.0.0.1" in d or "localhost" in d),
                    devices[0],
                )
                shot = capture_screenshot(device)
                if shot:
                    screenshot_paths[device] = shot

    # Lưu ra file nếu cần
    if args.output:
        data = all_reports if multi_device else all_reports[0]
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\nBug report da luu: {args.output}")

    # ── Agent 2: Report lên Jira ────────────────────────────────────────────────
    if not args.no_jira:
        if multi_device:
            from jira_reporter_agent import report_bugs_multi_device
            jira_results = report_bugs_multi_device(all_reports, screenshot_paths or None)
            print("\n" + "=" * 60)
            print("JIRA TICKETS")
            print("=" * 60)
            for r in jira_results:
                print_jira_result(r)
        else:
            from jira_reporter_agent import report_bug_to_jira
            first_shot = next(iter(screenshot_paths.values()), None)
            jira_result = report_bug_to_jira(all_reports[0], screenshot_path=first_shot)
            print_jira_result(jira_result)

    print("\nHoan thanh!")


if __name__ == "__main__":
    main()
