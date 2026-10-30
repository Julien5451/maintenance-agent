import argparse
import json

from .service import MaintenanceService


def main():
    parser = argparse.ArgumentParser(description="模拟设备报修 CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    create = commands.add_parser("create")
    create.add_argument("text")
    get = commands.add_parser("get")
    get.add_argument("task_id")
    respond = commands.add_parser("respond")
    respond.add_argument("task_id")
    respond.add_argument("action", choices=["provide_info", "confirm", "cancel"])
    respond.add_argument("--text")
    respond.add_argument("--draft-id")
    commands.add_parser("orders")
    args = parser.parse_args()
    svc = MaintenanceService()
    try:
        match args.command:
            case "health":
                result = svc.health()
            case "create":
                result = svc.create_task(args.text)
            case "get":
                result = svc.get_task(args.task_id)
            case "respond":
                result = svc.respond(args.task_id, args.action, args.text, args.draft_id)
            case "orders":
                result = svc.list_orders()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        svc.close()


if __name__ == "__main__":
    main()
