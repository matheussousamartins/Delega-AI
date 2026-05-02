from typing import Any


def normalize_phone(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.split("@", 1)[0]
    digits = "".join(char for char in raw if char.isdigit())
    return f"+{digits}" if digits else ""


def phone_variants(value: Any) -> list[str]:
    normalized = normalize_phone(value)
    if not normalized:
        return []

    variants = {normalized}
    digits = normalized[1:]

    if digits.startswith("55") and len(digits) in {12, 13}:
        country = digits[:2]
        ddd = digits[2:4]
        subscriber = digits[4:]
        if len(subscriber) == 8 and subscriber[:1] in {"6", "7", "8", "9"}:
            variants.add(f"+{country}{ddd}9{subscriber}")
        if len(subscriber) == 9 and subscriber.startswith("9"):
            variants.add(f"+{country}{ddd}{subscriber[1:]}")

    return sorted(variants)
