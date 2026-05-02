import sys

from whatsapp_task_agent.evolution_client import build_evolution_client


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scripts/send_test_message.py <number> [message]")

    number = sys.argv[1]
    message = sys.argv[2] if len(sys.argv) > 2 else "Teste Delega AI"

    client = build_evolution_client()
    if client is None:
        raise SystemExit("Evolution client is not configured")

    response = client.send_text(number=number, text=message)
    print("sent")
    print("status:", response.get("status"))
    print("messageType:", response.get("messageType"))


if __name__ == "__main__":
    main()
