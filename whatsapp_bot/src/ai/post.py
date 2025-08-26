# src/ai/post.py
import re
from typing import Iterable, Dict
from .schemas import MentionedItems, MentionedItem

SPANISH_NUMS = {
    "uno":1,"una":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,"siete":7,
    "ocho":8,"nueve":9,"diez":10
}

REMOVE_HINTS = re.compile(r"\b(quit(a|o)|elimin(a|á)|sac(a|á)|borra|sin)\b", re.I)
QTY_INLINE = re.compile(r"[xX]\s*(\d+)|(\d+)\s*(u(nidades?)?)?")

def normalize_qty(text: str, fallback: int | None = None) -> int | None:
    text_low = text.lower().strip()
    if m := QTY_INLINE.search(text):
        return int(next(g for g in m.groups() if g and g.isdigit()))
    return SPANISH_NUMS.get(text_low, fallback)

def consolidate_items(
    extracted: MentionedItems,
    history_pairs: Dict[str, int] | None = None,
    removals: Iterable[str] | None = None
) -> Dict[str, int]:
    """
    Regla: la cantidad más reciente prevalece.
    - history_pairs: estado previo del pedido {code: qty}
    - removals: códigos marcados para remover
    """
    result = dict(history_pairs or {})
    if removals:
        for code in removals:
            result.pop(code, None)
    for it in extracted.items:
        result[it.code] = it.qty
    return {k:v for k,v in result.items() if v > 0}
