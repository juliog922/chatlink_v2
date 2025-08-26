# src/api/app.py
from fastapi import FastAPI, HTTPException, UploadFile, Response, File, Form, Depends, Header
import secrets
from pydantic import BaseModel, constr
from typing import Optional, List
import tempfile
import qrcode
import base64
import logging
import os

from src.grpc.client import create_grpc_stub
from src.proto.whatsapp_pb2 import Empty, SendRequest, DeviceID
from src.core.database import get_postgres_session
from src.models.user import User
from src.mail.mail_handler import send_qr_email

app = FastAPI(
    title="WhatsApp Control API",
    version="1.0.0",
    docs_url=None,   # activa si quieres /docs
    redoc_url=None
)

# ---- gRPC stub lazy singleton ----
_STUB = None
def get_stub():
    global _STUB
    if _STUB is None:
        grpc_host = os.getenv("GRPC_HOST", "localhost")
        grpc_port = os.getenv("GRPC_PORT", None)
        if grpc_port:
            grpc_port = int(grpc_port)
        _STUB = create_grpc_stub(grpc_host, grpc_port)
    return _STUB


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

def auth_required(x_auth: str | None = Header(default=None, alias="X-Auth")):
    expected = os.getenv("AUTH_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=500, detail="AUTH_TOKEN no configurado en el entorno")
    # comparación en tiempo constante
    if not x_auth or not secrets.compare_digest(x_auth, expected):
        raise HTTPException(status_code=401, detail="missing/invalid X-Auth")

# ======= MODELOS =======
class LoginQrBody(BaseModel):
    to: constr(strip_whitespace=True, min_length=5, max_length=32) # type: ignore

class SendMessageBody(BaseModel):
    to: constr(strip_whitespace=True, min_length=5, max_length=64) # type: ignore
    text: constr(strip_whitespace=True, min_length=1, max_length=4096) # type: ignore
    from_jid: Optional[constr(strip_whitespace=True, min_length=5, max_length=128)] = None # type: ignore


# ======= ENDPOINTS EQUIVALENTES A COMANDOS =======

@app.post("/login", dependencies=[Depends(auth_required)])
def login():
    """
    Equivale a comando `login`:
    - Llama StartLogin.
    - Si status=code, devuelve el "code" y un PNG base64 del QR (por si alguien quiere renderizarlo).
    """
    stub = get_stub()
    try:
        resp = stub.StartLogin(Empty())
    except Exception as e:
        logging.exception("gRPC StartLogin falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    status = getattr(resp, "status", None)
    if status == "already_connected":
        return {"status": "already_connected"}

    if status == "code":
        # Generar QR PNG en memoria para devolverlo como base64 (conveniente para UI)
        img = qrcode.make(resp.code)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name, format="PNG")
            with open(tmp.name, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        try:
            os.remove(tmp.name)
        except Exception:
            pass
        return {"status": "code", "code": resp.code, "qr_png_base64": b64}

    if status == "success":
        return {"status": "success"}

    raise HTTPException(status_code=502, detail=f"Estado desconocido: {status}")


@app.post("/loginqr", dependencies=[Depends(auth_required)])
def login_qr(body: LoginQrBody):
    """
    Equivale a `loginqr`:
    - StartLogin -> si code: busca el email por `to` y envía el QR por email.
    """
    stub = get_stub()
    try:
        resp = stub.StartLogin(Empty())
    except Exception as e:
        logging.exception("gRPC StartLogin falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if resp.status == "already_connected":
        return {"status": "already_connected"}

    if resp.status == "code":
        session = get_postgres_session()
        try:
            user = User.get_by_phone(session, body.to)
            if not user:
                raise HTTPException(status_code=404, detail=f"No se encontró usuario con phone={body.to}")

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                qr_path = tmp.name
            try:
                img = qrcode.make(resp.code)
                img.save(qr_path, format="JPEG")
                send_qr_email(user.email, qr_path)
                return {"status": "sent", "email": user.email}
            finally:
                try:
                    os.remove(qr_path)
                except Exception:
                    pass
        finally:
            session.close()

    raise HTTPException(status_code=502, detail=f"Estado de login no manejado: {resp.status}")


@app.post("/loginqr_all", dependencies=[Depends(auth_required)])
def login_qr_all():
    """
    Equivale a `loginqr_all`: envía el QR a todos los admins.
    """
    stub = get_stub()
    try:
        resp = stub.StartLogin(Empty())
    except Exception as e:
        logging.exception("gRPC StartLogin falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if resp.status == "already_connected":
        return {"status": "already_connected"}

    if resp.status == "code":
        session = get_postgres_session()
        try:
            admins = User.get_admins(session) or []
            if not admins:
                return {"status": "no_admins"}
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                qr_path = tmp.name
            try:
                img = qrcode.make(resp.code)
                img.save(qr_path, format="JPEG")
                count = 0
                sent_to: List[str] = []
                for adm in admins:
                    send_qr_email(adm.email, qr_path)
                    sent_to.append(adm.email)
                    count += 1
                return {"status": "sent", "count": count, "emails": sent_to}
            finally:
                try:
                    os.remove(qr_path)
                except Exception:
                    pass
        finally:
            session.close()

    raise HTTPException(status_code=502, detail=f"Estado de login no manejado: {resp.status}")


@app.get("/devices", dependencies=[Depends(auth_required)])
def list_devices():
    """
    Equivale a `list`: devuelve jids registrados.
    """
    stub = get_stub()
    try:
        resp = stub.ListDevices(Empty())
    except Exception as e:
        logging.exception("gRPC ListDevices falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    return {"devices": [{"jid": d.jid} for d in resp.devices]}


@app.delete("/devices/{jid}", dependencies=[Depends(auth_required)])
def delete_device(jid: str):
    """
    Equivale a `delete`: elimina dispositivo por JID.
    """
    stub = get_stub()
    try:
        resp = stub.DeleteDevice(DeviceID(jid=jid))
    except Exception as e:
        logging.exception("gRPC DeleteDevice falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if resp.success:
        return {"status": "deleted", "jid": jid}
    raise HTTPException(status_code=400, detail=resp.error or "delete failed")


@app.post("/messages", dependencies=[Depends(auth_required)])
def send_message(body: SendMessageBody):
    """
    Equivale a `send`: envía texto. Valida que from_jid esté conectado.
    """
    stub = get_stub()

    if not body.from_jid:
        raise HTTPException(status_code=422, detail="from_jid es obligatorio")

    # validar from_jid
    try:
        devices = stub.ListDevices(Empty()).devices
        device_jids = {d.jid for d in devices}
    except Exception as e:
        logging.exception("gRPC ListDevices falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if body.from_jid not in device_jids:
        raise HTTPException(status_code=400, detail=f"from_jid {body.from_jid} no está conectado")

    req = SendRequest(to=body.to, text=body.text, from_jid=body.from_jid)
    try:
        resp = stub.SendMessage(req)
    except Exception as e:
        logging.exception("gRPC SendMessage falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if resp.success:
        return {"status": "sent", "to": body.to}
    raise HTTPException(status_code=400, detail=resp.error or "send failed")


@app.post("/files", dependencies=[Depends(auth_required)])
def send_file(
    to: str = Form(...),
    from_jid: Optional[str] = Form(None),
    file: UploadFile = File(...)
):
    """
    Equivale a `sendfile`: multipart/form-data con campos to, from_jid y file.
    """
    stub = get_stub()

    # validar from_jid
    if not from_jid:
        raise HTTPException(status_code=422, detail="from_jid es obligatorio")

    try:
        devices = stub.ListDevices(Empty()).devices
        device_jids = {d.jid for d in devices}
    except Exception as e:
        logging.exception("gRPC ListDevices falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if from_jid not in device_jids:
        raise HTTPException(status_code=400, detail=f"from_jid {from_jid} no está conectado")

    # leer binario
    binary = file.file.read()
    req = SendRequest(
        to=to,
        text=file.filename or "file",
        binary=binary,
        filename=file.filename or "upload.bin",
        from_jid=from_jid,
    )

    try:
        resp = stub.SendMessage(req)
    except Exception as e:
        logging.exception("gRPC SendMessage falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if resp.success:
        return {"status": "sent", "to": to, "filename": file.filename}
    raise HTTPException(status_code=400, detail=resp.error or "send file failed")

@app.delete("/devices/{jid}", dependencies=[Depends(auth_required)])
def delete_device(jid: str):
    """
    Equivale al comando `delete --jid <JID>`:
    - Elimina el dispositivo por JID vía gRPC.
    - 204 si se elimina, 400 si falla (mensaje de error del backend).
    """
    stub = get_stub()
    try:
        resp = stub.DeleteDevice(DeviceID(jid=jid))
    except Exception as e:
        logging.exception("gRPC DeleteDevice falló")
        raise HTTPException(status_code=502, detail=f"gRPC error: {e}")

    if resp.success:
        return Response(status_code=204)
    raise HTTPException(status_code=400, detail=resp.error or "delete failed")
