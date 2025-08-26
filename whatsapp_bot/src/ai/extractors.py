import re
from typing import Optional, Tuple, List


import json, re
from typing import Optional, List, Tuple

def extract_mentioned_products(text: str) -> Optional[List[Tuple[str, str]]]:
    # 1) intenta JSON directo
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "items" in obj and isinstance(obj["items"], list):
            out = []
            for pair in obj["items"]:
                if (isinstance(pair, (list, tuple)) and len(pair) == 2
                        and isinstance(pair[0], str) and isinstance(pair[1], str)):
                    out.append((pair[0].strip(), pair[1].strip()))
            return out or None
    except Exception:
        pass

    # 2) fallback: busca el array items por regex y vuelve a intentar
    m = re.search(r'\{\s*"items"\s*:\s*(\[[\s\S]*?\])\s*\}', text)
    if m:
        try:
            arr = json.loads(m.group(1))
            out = []
            for pair in arr:
                if (isinstance(pair, (list, tuple)) and len(pair) == 2):
                    out.append((str(pair[0]).strip(), str(pair[1]).strip()))
            return out or None
        except Exception:
            return None

    # 3) último recurso: tu regex original
    pattern = r'\[\s*"([^"]+)"\s*,\s*"([^"]*)"\s*\]'
    matches = re.findall(pattern, text)
    if matches:
        return [(c.strip(), q.strip()) for c, q in matches] or None
    return None

def extract_response_text(text: str) -> Optional[str]:
    # intenta JSON
    try:
        obj = json.loads(text)
        if obj.get("responder") is True and isinstance(obj.get("respuesta"), str):
            return obj["respuesta"]
    except Exception:
        pass
    # fallback regex tolerante a saltos de línea
    m = re.search(r'"responder"\s*:\s*true\s*,\s*"respuesta"\s*:\s*"([\s\S]*?)"\s*}', text)
    return m.group(1) if m else None


def is_order(output_text: str) -> bool:
    return '"order": true' in output_text.lower()


def is_order_confirmation(message: str) -> bool:
    pattern = r"(es\s*correct[oa]*)"
    return bool(re.search(pattern, message.lower()))
