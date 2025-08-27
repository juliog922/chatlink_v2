import os
import logging
import grpc
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from src.proto.whatsapp_pb2 import Empty, MessageEvent
from src.core.database import get_sqlserver_session, get_postgres_session
from src.grpc.handlers import send_message, delete_device, login_and_send_qr
from src.ai.agent import handle_incoming_message
from src.media.ocr import extract_text_from_image
from src.media.audio import transcribe_audio
from src.media.documents import (
    extract_text_from_csv,
    extract_text_from_docx,
    extract_text_from_pdf,
    extract_text_from_txt,
    extract_text_from_xlsx,
)
from src.models.user import User
from src.models.message import Message
from src.models.client import Cliente


def normalize_number(raw):
    return raw.split(":")[0].lstrip("+")


def handle_admin_command(
    msg: MessageEvent,
    sender_norm: str,
    receiver_norm: str,
    stub,
    postgres_session: Session,
):
    admins = [admin.phone for admin in User.get_admins(postgres_session)]

    is_to_admin = any(receiver_norm.endswith(admin) for admin in admins)
    is_user_self = sender_norm == receiver_norm

    if not is_to_admin or is_user_self:
        return False

    message_text = msg.text.lower().strip()

    if message_text.startswith("logout"):
        user = User.get_by_phone_fuzzy(postgres_session, sender_norm)
        if user:
            logging.info(f"Logout requested by {sender_norm}")
            delete_device(stub, sender_norm)
        else:
            logging.warning(f"Invalid logout attempt from {sender_norm}")
        return True

    elif message_text.startswith("login"):
        user = User.get_by_phone_fuzzy(postgres_session, sender_norm)
        if user:
            logging.info(f"Login requested by {sender_norm}")
            login_and_send_qr(stub, sender_norm)
        else:
            logging.warning(f"Invalid login attempt from {sender_norm}")
        return True

    if User.user_exists(postgres_session, sender_norm):
        help_text = (
            "📋 *Comandos disponibles:*\n\n"
            "🔐 `login`\n"
            "Inicia sesion y envia QR al email.\n\n"
            "🚪 `logout`\n"
            "Finaliza tu sesion.\n\n"
        )
        send_message(stub, sender_norm, help_text, receiver_norm)
        return True

    return False


def stream_messages(stub):
    logging.info("Connecting to WhatsApp message stream...")
    base_dir = "media"
    os.makedirs(base_dir, exist_ok=True)

    sqlserver_session = get_sqlserver_session()
    postgres_session = get_postgres_session()

    try:

        for msg in stub.StreamMessages(Empty()):
            sender = getattr(msg, "from").split("@")[0].split(":")[0]
            receiver = msg.to.split(":")[0]
            sender_norm = normalize_number(sender)
            receiver_norm = normalize_number(receiver)

            logging.info(f"New message: {sender} → {receiver} ({msg.timestamp})")

            if handle_admin_command(
                msg, sender_norm, receiver_norm, stub, postgres_session
            ):
                continue

            matched_id, direction, message_type, final_content, saved_path = \
            store_message_if_applicable(msg, sender, receiver, postgres_session, sqlserver_session, base_dir)

            if message_type == "text" and final_content:
                preview = final_content.replace("\n", " ")[:200]
                logging.info(f"Message content (normalized): {preview}")
            elif msg.binary:
                logging.info(
                    f"Binary message received: {msg.filename or 'unnamed_file'}; "
                    f"saved to {saved_path or 'N/A'}; bytes={len(msg.binary)}"
                )
            elif msg.text.strip():
                logging.info(f"Message content: {msg.text.strip()}")

    except grpc.RpcError as e:
        logging.error(f"gRPC stream error: {e.code().name} - {e.details()}")

    finally:
        sqlserver_session.close()
        postgres_session.close()


def store_message_if_applicable(
    msg,
    sender,
    receiver,
    postgres_session: Session,
    sqlserver_session: Session,
    base_dir: str,
):
    direction = None
    matched_cliente = Cliente.get_by_telefono(sqlserver_session, sender)
    direction = "received" if matched_cliente else None

    if not matched_cliente:
        logging.info("Is client Message")
        matched_cliente = Cliente.get_by_telefono(sqlserver_session, receiver)
        direction = "sent" if matched_cliente else None

    if not matched_cliente:
        return None, None, None, None, None

    matched_id = matched_cliente.codigo_cliente
    message_type = "text"
    content = msg.text

    # Buscar el user por teléfono
    user = User.get_by_phone_fuzzy(postgres_session, sender) or User.get_by_phone_fuzzy(
        postgres_session, receiver
    )
    saved_path = None

    if msg.binary:
        message_type = "media"
        filename = msg.filename or f"file_{msg.timestamp}.bin"
        ext = os.path.splitext(filename)[1].lower()

        subdir = (
            "images"
            if ext in [".jpg", ".jpeg", ".png", ".webp"]
            else (
                "audio"
                if ext in [".mp3", ".ogg", ".wav", ".opus"]
                else "video" if ext in [".mp4", ".avi", ".mkv"] else "documents"
            )
        )

        full_dir = os.path.join(base_dir, subdir)
        os.makedirs(full_dir, exist_ok=True)
        file_path = os.path.join(full_dir, filename)
        saved_path = file_path
        text = ""
        try:
            with open(file_path, "wb") as f:
                f.write(msg.binary)
            logging.info(f"Saved media file: {file_path}")

            if subdir == "images":
                text = extract_text_from_image(msg.binary)
                if text:
                    content = text
                    message_type = "text"
            elif subdir == "audio":
                text = transcribe_audio(msg.binary, extension=ext)
                if text:
                    content = text
                    message_type = "text"
            elif subdir == "documents":
                text = None
                if ext == ".pdf":
                    text = extract_text_from_pdf(file_path)
                elif ext == ".docx":
                    text = extract_text_from_docx(file_path)
                elif ext == ".txt":
                    text = extract_text_from_txt(file_path)
                elif ext == ".csv":
                    text = extract_text_from_csv(file_path)
                elif ext == ".xlsx":
                    text = extract_text_from_xlsx(file_path)

                if text and text.strip():
                    content = text.strip()
                    message_type = "text"
            
            logging.info(f"Extracted text: {text}")
        except Exception as e:
            logging.error(f"Error saving media: {e}")

    Message.create(
        session=postgres_session,
        client_id=matched_id,
        client_phone=receiver if direction == "sent" else sender,
        direction=direction,
        type_=message_type,
        content=content.replace("\n", " "),
        user_id=user.id,
        user_phone=sender if direction == "sent" else receiver,
        timestamp=parse_flexible_timestamp(msg.timestamp),
    )
    return matched_id, direction, message_type, content, saved_path


def parse_flexible_timestamp(ts: str) -> datetime:
    s = str(ts).strip()
    if s.endswith("Z"):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
    else:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d_%H%M%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    logging.warning(f"[ts] formato desconocido {s!r}; uso now()")
    return datetime.now(timezone.utc)