import logging
import tempfile
import time
from typing import List, Optional
from langchain_ollama import ChatOllama
from langchain.schema import HumanMessage
from sqlalchemy import and_, func
from sqlalchemy.orm import Session
from PIL.Image import Image
from sqlalchemy.orm import aliased
from sqlalchemy import select, desc
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta, timezone

from src.ai.extractors import (
    extract_response_text,
    extract_mentioned_products,
    is_order,
    is_order_confirmation
)
from src.ai.utils import update_order, confirmed_order, order_to_xlsx, order_to_pdf
from src.core.database import get_postgres_session, get_sqlserver_session
from src.models.message import Message
from src.models.user import User
from src.models.product import Articulo
from src.models.client import Cliente
from src.grpc.handlers import send_message, send_file
from src.mail.mail_handler import notify_order_by_email
from src.ai.pipeline import build_chat
from src.ai.prompts import *
from src.ai.utils import (
    update_order, confirmed_order, order_to_xlsx, order_to_pdf
)
import re, logging

load_dotenv()
MIN_MINUTES = int(os.getenv("UNATTENDED_MINUTES_MIN", 15))
MAX_MINUTES = int(os.getenv("UNATTENDED_MINUTES_MAX", 30))

OLLAMA_URL = os.getenv("OLLAMA_URL", None)

chat = build_chat(OLLAMA_URL)

def handle_incoming_message(
    postgre_session: Session,
    sqlserver_session: Session,
    stub,
    receiver: str,
    sender: str,
    message_text: str,
    chat=chat,
):
    logging.info("Handling incoming message for AI processing")

    cliente = Cliente.get_by_telefono(sqlserver_session, sender)
    if not cliente:
        logging.warning(f"There is not a client with phone: {sender}")
        return

    comercial = User.get_by_phone(postgre_session, receiver)
    if not comercial:
        logging.warning(f"There is not user with phone: {receiver}")
        return

    comercial_name: str = comercial.name or "el vendedor"

    stmt = (
        select(Message.direction, Message.content)
        .where(Message.client_id == cliente.codigo_cliente)
        .order_by(desc(Message.timestamp))
        .limit(6)
    )
    messages: List[Message] = postgre_session.execute(stmt).all()[::-1]

    history: str = "\n".join(
        [
            f"{'Cliente:' if d == 'sent' else 'Comercial:'}: {c}"
            for d, c in messages
            if c
        ]
    )

    is_order_prompt_text: str = is_order_prompt(message_text)
    is_order_raw_response: str = chat.invoke(
        [HumanMessage(content=is_order_prompt_text)]
    ).content.strip()
    logging.info(f"Is an order: {is_order(is_order_raw_response)}")
    if is_order(is_order_raw_response):
        logging.info(f"Is an order confirmation: {is_order_confirmation(message_text)}")
        if is_order_confirmation(message_text):
            for message in messages:
                logging.info(
                    f"message direction: {message.direction} \ message content: {message.content}"
                )
            confirmed_order_text: str = confirmed_order(messages)
            logging.info(f"confirmed_order_text: {confirmed_order_text}")
            updated_confirmed_order_csv_path: Optional[str] = order_to_xlsx(
                confirmed_order_text
            )
            updated_confirmed_order_pdf_path: str = order_to_pdf(confirmed_order_text)

            notify_order_by_email(
                user=comercial,
                client=cliente,
                phone=sender,
                csv_path=updated_confirmed_order_csv_path,
            )
            send_file(stub, sender, updated_confirmed_order_pdf_path, from_jid=receiver)
        else:
            mentioned_products_prompt_text: str = mentioned_products_prompt(
                history, message_text
            )
            mentioned_products_raw_response: str = chat.invoke(
                [HumanMessage(content=mentioned_products_prompt_text)]
            ).content.strip()
            if mentioned_products := extract_mentioned_products(
                mentioned_products_raw_response
            ):
                logging.info(f"Mentioned products: {mentioned_products}")
                img: Image | None = update_order(sqlserver_session, mentioned_products)
                if img:
                    send_message(
                        stub,
                        sender,
                        "Confirma si el pedido es correcto respondiendo con *Es correcto*.\
                        Se lo pasaremos a tu comercial que se encargará de todo o te contactará si hay alguna duda.\
                        En caso de que no sea correcto, sientete libre de repetirme el pedido o indicar unicamente las correcciones\
                        [Este mensaje fue generado automáticamente por un asistente en versión de pruebas]",
                        from_jid=receiver,
                    )
                    
                    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M")
                    filename = f"pedido_{timestamp}.jpg"
                    tmp_dir = tempfile.gettempdir()
                    filepath = os.path.join(tmp_dir, filename)

                    img.save(filepath, format="JPEG")

                    send_file(stub, sender, filepath=filepath, from_jid=receiver)
                    del img
                    os.remove(filepath)
            else:
                chat_prompt_text: str = chat_prompt(
                    comercial_name, history, message_text
                )
                chat_raw_response: str = chat.invoke(
                    [HumanMessage(content=chat_prompt_text)]
                ).content.strip()
                chat_response: str | None = extract_response_text(chat_raw_response)
                if chat_response and len(chat_response.strip()) > 0:
                    chat_response += "\n[Este mensaje fue generado automáticamente por un asistente en versión de pruebas]"
                    send_message(stub, sender, chat_response, from_jid=receiver)
                    logging.info("IA Response successfully sent")
                else:
                    logging.info("There is not IA response")
    else:
        chat_prompt_text: str = chat_prompt(comercial_name, history, message_text)
        chat_raw_response: str = chat.invoke(
            [HumanMessage(content=chat_prompt_text)]
        ).content.strip()
        chat_response: str | None = extract_response_text(chat_raw_response)
        if chat_response and len(chat_response.strip()) > 0:
            chat_response += "\n[Este mensaje fue generado automáticamente por un asistente en versión de pruebas]"
            send_message(stub, sender, chat_response, from_jid=receiver)
            logging.info("IA Response successfully sent")
        else:
            logging.info("There is not IA response")


def search_products(sqlserver_session: Session, keywords: list[str]) -> str:
    if not keywords:
        return ""

    articulos = Articulo.get_by_words_list(sqlserver_session, keywords)

    if not articulos:
        return ""

    lines = []
    for art in articulos:
        lines.append(
            f"- Codigo de Articulo: {art.codigo} / Descripcion: {art.descripcion1}"
        )

    logging.info("\n".join(lines))
    return "\n".join(lines)


def process_unattended_messages_loop(stub):
    while True:
        logging.info("process_unattended_messages_loop: checking lasts messages")

        postgres_session = get_postgres_session()
        sqlserver_session = get_sqlserver_session()

        try:
            # Aliased para evitar conflictos
            MessageAlias = aliased(Message)

            # Subconsulta que obtiene el último timestamp por client_id
            subq = (
                postgres_session.query(
                    MessageAlias.client_id,
                    func.max(MessageAlias.timestamp).label("latest"),
                )
                .filter(MessageAlias.direction == "received")
                .group_by(MessageAlias.client_id)
                .subquery()
            )

            # Join para obtener los mensajes completos
            last_msgs = (
                postgres_session.query(MessageAlias)
                .join(
                    subq,
                    and_(
                        MessageAlias.client_id == subq.c.client_id,
                        MessageAlias.timestamp == subq.c.latest,
                    ),
                )
                .filter(MessageAlias.direction == "received")
                .all()
            )
            now = datetime.now()

            for last_msg in last_msgs:
                # Verificar si ya se respondió
                response = (
                    postgres_session.query(Message)
                    .filter(
                        and_(
                            Message.client_id == last_msg.client_id,
                            Message.direction == "sent",
                            Message.timestamp > last_msg.timestamp,
                        )
                    )
                    .first()
                )

                if response:
                    continue  # Ya respondido

                if not last_msg.content:
                    continue  # No hay texto para procesar

                # Verificar rango de tiempo
                age = now - last_msg.timestamp
                if age < timedelta(minutes=MIN_MINUTES) or age > timedelta(
                    minutes=MAX_MINUTES
                ):
                    continue  # Fuera del rango definido

                # Obtener teléfono del cliente
                client_phone = last_msg.client_phone
                client = Cliente.get_by_telefono(sqlserver_session, client_phone)
                if not client:
                    continue

                # Obtener usuario asignado
                user = (
                    postgres_session.query(User)
                    .filter(User.id == last_msg.user_id)
                    .first()
                )
                if not user:
                    logging.info(f"Cliente {last_msg.client_id} sin usuario asignado")
                    continue

                logging.info(f"Enviando respuesta IA a cliente {last_msg.client_id}")
                handle_incoming_message(
                    postgres_session,
                    sqlserver_session,
                    stub,
                    user.phone,
                    client_phone,
                    last_msg.content,
                )

        except Exception as e:
            logging.error(f"Error en el loop de mensajes no atendidos: {e}")

        finally:
            postgres_session.close()
            sqlserver_session.close()

        time.sleep(60)

def to_aware_utc(dt: datetime) -> datetime:
    """Convierte cualquier datetime a timezone-aware en UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Asumimos que los naive son UTC. Si en tu BD están en hora local,
        # reemplaza por la zona correcta y luego .astimezone(timezone.utc)
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def process_unattended_messages_loop(stub):
    while True:
        logging.info("process_unattended_messages_loop: checking last messages")

        postgres_session = get_postgres_session()
        sqlserver_session = get_sqlserver_session()

        try:
            MessageAlias = aliased(Message)

            subq = (
                postgres_session.query(
                    MessageAlias.client_id,
                    func.max(MessageAlias.timestamp).label("latest"),
                )
                .filter(MessageAlias.direction == "received")
                .group_by(MessageAlias.client_id)
                .subquery()
            )

            last_msgs = (
                postgres_session.query(MessageAlias)
                .join(
                    subq,
                    and_(
                        MessageAlias.client_id == subq.c.client_id,
                        MessageAlias.timestamp == subq.c.latest,
                    ),
                )
                .filter(MessageAlias.direction == "received")
                .all()
            )

            # AHORA: aware en UTC
            now = datetime.now(timezone.utc)

            for last_msg in last_msgs:
                # Defensive: normalizamos el timestamp del mensaje a aware UTC
                last_msg_ts = to_aware_utc(last_msg.timestamp)

                # ¿Ya se respondió?
                response = (
                    postgres_session.query(Message)
                    .filter(
                        and_(
                            Message.client_id == last_msg.client_id,
                            Message.direction == "sent",
                            Message.timestamp > last_msg_ts,
                        )
                    )
                    .first()
                )
                if response:
                    continue

                if not last_msg.content:
                    continue

                # Rango de tiempo (todo en UTC-aware)
                age = now - last_msg_ts
                if age < timedelta(minutes=MIN_MINUTES) or age > timedelta(minutes=MAX_MINUTES):
                    continue

                client_phone = last_msg.client_phone
                client = Cliente.get_by_telefono(sqlserver_session, client_phone)
                if not client:
                    continue

                user = postgres_session.query(User).filter(User.id == last_msg.user_id).first()
                if not user:
                    logging.info(f"Cliente {last_msg.client_id} sin usuario asignado")
                    continue

                logging.info(f"🤖 Enviando respuesta IA a cliente {last_msg.client_id}")
                handle_incoming_message(
                    postgres_session,
                    sqlserver_session,
                    stub,
                    user.phone,
                    client_phone,
                    last_msg.content,
                )

        except Exception as e:
            logging.exception(f"Error en el loop de mensajes no atendidos: {e}")

        finally:
            postgres_session.close()
            sqlserver_session.close()

        time.sleep(60)


def search_simulated_products(fake_index: dict[str, str], keywords: list[str]) -> str:
    if not keywords:
        return ""

    results = []
    for key in keywords:
        for code_or_kw, desc in fake_index.items():
            if key.lower() in code_or_kw.lower() or key.lower() in desc.lower():
                results.append(
                    f"- Codigo de Articulo: {code_or_kw} / Descripcion: {desc}"
                )
    return "\n".join(results)
