# manage.py (fragmentos clave)
import threading
import os
from dotenv import load_dotenv
from src.config.logging_setup import setup_logging
from src.cli.parser import build_parser
from src.grpc.client import create_grpc_stub
from src.whatsapp.stream import stream_messages
from src.ai.agent import process_unattended_messages_loop
from src.grpc.handlers import (
    login,
    login_and_send_qr,
    list_devices,
    send_message,
    send_file,
    delete_device,
    login_and_send_qr_to_all_admins,
)

def _start_api_server_in_thread():
    import uvicorn
    from src.api.app import app

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="info")

    t = threading.Thread(target=_run, daemon=True, name="uvicorn-api")
    t.start()
    return t


def main():
    load_dotenv()
    setup_logging()

    parser = build_parser()
    args = parser.parse_args()

    grpc_host = os.getenv("GRPC_HOST", "localhost")
    grpc_port = os.getenv("GRPC_PORT", None)
    if grpc_port:
        grpc_port = int(grpc_port)
    stub = create_grpc_stub(grpc_host, grpc_port)

    if args.cmd == "login":
        login(stub)
    elif args.cmd == "loginqr":
        login_and_send_qr(stub, args.to)
    elif args.cmd == "loginqr_all":
        login_and_send_qr_to_all_admins(stub)
    elif args.cmd == "list":
        list_devices(stub)
    elif args.cmd == "listen":
        # NO TOCAR: comportamiento original
        ai_thread = threading.Thread(
            target=process_unattended_messages_loop, args=(stub,), daemon=True, name="ai-loop"
        )
        ai_thread.start()
        stream_messages(stub)
    elif args.cmd == "send":
        send_message(stub, args.to, args.text, from_jid=args.from_jid)
    elif args.cmd == "sendfile":
        send_file(stub, args.to, args.file, from_jid=args.from_jid)
    elif args.cmd == "delete":
        delete_device(stub, args.jid)
    elif args.cmd == "start":
        # 1) API
        _start_api_server_in_thread()
        # 2) IA en hilo
        ai_thread = threading.Thread(
            target=process_unattended_messages_loop, args=(stub,), daemon=True, name="ai-loop"
        )
        ai_thread.start()
        # 3) listener (bloqueante)
        stream_messages(stub)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
