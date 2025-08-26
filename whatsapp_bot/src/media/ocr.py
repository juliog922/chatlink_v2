import logging
import numpy as np
import cv2
from paddleocr import PaddleOCR

# --- parámetros anti-ruido (tunea a tu dataset) ---
MIN_SCORE = 0.60            # subido un poco
MIN_BOX_AREA = 150.0        # ignora cajas muy pequeñas
ROW_BIN = 34                # tamaño de agrupación vertical
PUNCT_ONLY = set([":", ";", "|", "•", "·", ".", ",", "-", "_", "—", "–", "+", "&", "·", "•"])
ALLOWED_CHARS_RE = r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9/\-:%×x]"

def polygon_area(poly):
    x = [p[0] for p in poly]
    y = [p[1] for p in poly]
    return 0.5 * abs(
        x[0]*y[1] + x[1]*y[2] + x[2]*y[3] + x[3]*y[0]
        - (y[0]*x[1] + y[1]*x[2] + y[2]*x[3] + y[3]*x[0])
    )

def _clean_token(t: str) -> str:
    # normaliza unicode y corrige signos confusos comunes
    t = (t or "").strip()
    if not t:
        return t
    # normalizaciones típicas de OCR
    t = t.replace("×", "x").replace("–", "-").replace("—", "-")
    t = t.replace("¡", "").replace("¿", "")
    # corrige diéresis mal detectadas como acentos (opcional en ES)
    t = (t
         .replace("ä", "á").replace("Ä", "Á")
         .replace("ë", "é").replace("Ë", "É")
         .replace("ï", "í").replace("Ï", "Í")
         .replace("ö", "ó").replace("Ö", "Ó")
         .replace("ü", "ú").replace("Ü", "Ú"))
    return t

def _char_quality_ratio(t: str) -> float:
    # % de caracteres "permitidos" (letras ES, dígitos y signos útiles)
    import re
    if not t:
        return 0.0
    ok = len(re.findall(ALLOWED_CHARS_RE, t))
    return ok / max(1, len(t))

def compose_text_by_rows(items):
    """
    items: [(y_min, x_min, text, score, area)]
    Filtra ruido y compone líneas legibles.
    """
    import re

    filtered = []
    for y, x, t, s, a in items:
        t = _clean_token(t)
        if not t:
            continue

        # elimina tokens que son solo puntuación o de 1 char no alfanumérico
        if t in PUNCT_ONLY:
            continue
        if len(t) == 1 and not t.isalnum():
            continue

        # filtra por score/área, permitiendo dígitos sueltos un poco más laxos
        if s < MIN_SCORE or a < MIN_BOX_AREA:
            if not (len(t) == 1 and t.isdigit() and s >= MIN_SCORE - 0.08):
                continue

        # filtra tokens con baja "calidad" de caracteres (símbolos raros)
        if _char_quality_ratio(t) < 0.55 and len(t) <= 3:
            continue

        filtered.append((y, x, t, s))

    if not filtered:
        return ""

    # agrupación por filas
    bins = {}
    for y, x, t, s in filtered:
        row_key = int(y // ROW_BIN)
        bins.setdefault(row_key, []).append((x, t, s, y))

    lines_out = []
    for row_key in sorted(bins.keys()):
        row = sorted(bins[row_key], key=lambda z: z[0])
        parts = []
        for i, (x, t, s, y) in enumerate(row):
            if not parts:
                parts.append(t)
            else:
                if t in {".", ",", ":", ";", "%"}:
                    parts[-1] = parts[-1] + t
                elif parts[-1] and parts[-1][-1] in {".", ",", ":", ";", "%"}:
                    parts.append(t)
                else:
                    parts.append(" " + t)
        line = "".join(parts).strip()

        # elimina líneas residuales con casi solo signos
        if line and _char_quality_ratio(line) >= 0.6:
            lines_out.append(line)

    text = "\n".join(lines_out).strip()

    # --- post-normalización: compacta saltos y arregla espacios antes/después de signos ---
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*([,:;%])\s*", r"\1 ", text)  # " : " -> ": "
    text = re.sub(r"\s*([/xX-])\s*", r" \1 ", text) # separadores útiles
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(?:\n\s*){2,}", "\n", text)

    # Heurística: si la mayoría de líneas son cortas y acaban en conectores, colapsa en una sola
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if lines and all(len(ln) < 40 for ln in lines) and len(lines) <= 4:
        text = " ".join(lines)
        text = re.sub(r"\s{2,}", " ", text).strip()

    return text

# --- tu OCR model igual que lo tienes ---
ocr_model = PaddleOCR(
    lang="es",
    use_angle_cls=True,
    use_gpu=True,
    det_db_thresh=0.3,
    det_db_box_thresh=0.5,
    det_db_unclip_ratio=1.8,
    drop_score=0.3,
    rec_batch_num=8,
    cls_batch_num=8,
    max_text_length=256,
    show_log=False
)

def preprocess(image: np.ndarray) -> np.ndarray:
    # 2) Preprocesado adaptativo
    # a) Desruido suave
    den = cv2.fastNlMeansDenoisingColored(image, None, 5, 5, 7, 21)

    # b) Pasar a gray y realzar contraste con CLAHE
    gray = cv2.cvtColor(den, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    gray = clahe.apply(gray)

    # c) Binarización local (mejor que Otsu cuando hay sombras/fondos claros)
    bin_local = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 10
    )

    # d) Morfología ligera para cerrar huecos en trazos
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
    bin_morph = cv2.morphologyEx(bin_local, cv2.MORPH_CLOSE, kernel, iterations=1)

    # e) Si la imagen es pequeña, escalar (OCR agradece > ~800px lado largo)
    h, w = bin_morph.shape[:2]
    scale = 1.5 if max(h, w) < 900 else 1.0
    if scale != 1.0:
        bin_morph = cv2.resize(bin_morph, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_CUBIC)

    # Volver a BGR porque PaddleOCR admite np.ndarray BGR/GRAY pero
    # mantenemos consistente con el resto del pipeline
    return cv2.cvtColor(bin_morph, cv2.COLOR_GRAY2BGR)

def extract_text_from_image(image_bytes: bytes) -> str:
    try:
        np_img = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(np_img, cv2.IMREAD_COLOR)

        proc = preprocess(img)

        # intento 1: con preprocesado
        result = ocr_model.ocr(proc, cls=True)

        # intento 2: sin preprocesado si poco contenido
        num_lines = sum(len(block) for block in result if block)
        if num_lines <= 1:
            result = ocr_model.ocr(img, cls=True)

        items = []
        for block in result:
            for line in block:
                box, (text, score) = line
                y_min = min(pt[1] for pt in box)
                x_min = min(pt[0] for pt in box)
                area = polygon_area(box)
                items.append((y_min, x_min, text, float(score), float(area)))

        extracted_text = compose_text_by_rows(items)
        return extracted_text

    except Exception as e:
        logging.exception(f"OCR error: {e}")
        return ""

