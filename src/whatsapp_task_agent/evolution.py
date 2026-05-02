import re
from typing import Any

from pydantic import BaseModel

from whatsapp_task_agent.phone import normalize_phone
from whatsapp_task_agent.schemas import WhatsAppInput


class NormalizedEvolutionEvent(BaseModel):
    from_phone: str
    message: str | None = None
    provider_message_id: str | None = None
    message_type: str
    media_url: str | None = None
    media_base64: str | None = None
    mimetype: str | None = None
    file_name: str | None = None
    from_me: bool = False
    contact_name: str | None = None
    contact_phone: str | None = None
    quoted_message_body: str | None = None

    @property
    def is_text(self) -> bool:
        return self.message_type == "text" and bool(self.message)

    @property
    def is_audio(self) -> bool:
        return self.message_type == "audio"

    @property
    def is_contact(self) -> bool:
        return self.message_type == "contact" and bool(self.contact_phone)

    def to_whatsapp_input(self) -> WhatsAppInput:
        if not self.message:
            raise ValueError("event_has_no_text_message")
        return WhatsAppInput(
            from_phone=self.from_phone,
            message=self.message,
            provider_message_id=self.provider_message_id,
            quoted_message_body=self.quoted_message_body,
        )


def normalize_evolution_payload(payload: dict[str, Any] | list[Any]) -> NormalizedEvolutionEvent:
    payload = _coerce_mapping(payload)
    data = _coerce_mapping(payload.get("data") or payload)
    key = data.get("key") or {}
    message = data.get("message") or {}
    from_me = bool(key.get("fromMe") or data.get("fromMe") or payload.get("fromMe"))

    remote_jid = (
        key.get("remoteJid")
        or data.get("remoteJid")
        or data.get("sender")
        or payload.get("sender")
        or payload.get("from")
    )
    from_phone = normalize_phone(remote_jid)
    if not from_phone:
        raise ValueError("evolution_payload_invalid_sender")

    provider_message_id = (
        key.get("id")
        or data.get("messageId")
        or data.get("id")
        or payload.get("messageId")
        or payload.get("id")
    )

    text = _extract_text(message, data)
    media_url = _extract_media_url(message, data)
    media_base64 = _extract_media_base64(message, data)
    mimetype = _extract_mimetype(message, data)
    message_type = _detect_message_type(message, data, text=text)

    contact_name, contact_phone = _extract_contact(message, data)
    quoted_message_body = _extract_quoted_message_body(message, data)

    return NormalizedEvolutionEvent(
        from_phone=from_phone,
        message=text,
        provider_message_id=provider_message_id,
        message_type=message_type,
        media_url=media_url,
        media_base64=media_base64,
        mimetype=mimetype,
        file_name=data.get("fileName") or _nested_get(message, "audioMessage", "fileName"),
        from_me=from_me,
        contact_name=contact_name,
        contact_phone=contact_phone,
        quoted_message_body=quoted_message_body,
    )


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
        return {}
    return {}


def _extract_text(message: dict[str, Any], data: dict[str, Any]) -> str | None:
    candidates = [
        message.get("conversation"),
        _nested_get(message, "extendedTextMessage", "text"),
        _nested_get(message, "textMessage", "text"),
        data.get("text"),
        data.get("messageText"),
        data.get("body"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _extract_media_url(message: dict[str, Any], data: dict[str, Any]) -> str | None:
    candidates = [
        _nested_get(message, "audioMessage", "url"),
        _nested_get(message, "audio", "url"),
        data.get("mediaUrl"),
        data.get("url"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _extract_media_base64(message: dict[str, Any], data: dict[str, Any]) -> str | None:
    candidates = [
        data.get("base64"),
        data.get("mediaBase64"),
        data.get("fileBase64"),
        _nested_get(message, "base64"),
        _nested_get(message, "audioMessage", "base64"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _extract_mimetype(message: dict[str, Any], data: dict[str, Any]) -> str | None:
    candidates = [
        data.get("mimetype"),
        data.get("mimeType"),
        _nested_get(message, "audioMessage", "mimetype"),
        _nested_get(message, "audioMessage", "mimeType"),
        _nested_get(message, "audio", "mimetype"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _extract_contact(message: dict[str, Any], data: dict[str, Any]) -> tuple[str | None, str | None]:
    # Single contact message
    contact_msg = _nested_get(message, "contactMessage")
    if isinstance(contact_msg, dict):
        return _parse_contact_entry(contact_msg)

    # Array of contacts — use the first one
    contacts_msg = _nested_get(message, "contactsArrayMessage", "contacts")
    if isinstance(contacts_msg, list) and contacts_msg:
        return _parse_contact_entry(contacts_msg[0])

    # Evolution may flatten the vcard directly on data
    vcard = data.get("vcard") or data.get("vCard")
    display_name = data.get("displayName") or data.get("contactName")
    if vcard:
        name, phone = _parse_vcard(vcard)
        return (display_name or name, phone)

    return None, None


def _parse_contact_entry(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    display_name = entry.get("displayName") or entry.get("name")
    vcard = entry.get("vcard") or entry.get("vCard") or ""
    name, phone = _parse_vcard(vcard)
    return (display_name or name, phone)


def _parse_vcard(vcard: str) -> tuple[str | None, str | None]:
    name: str | None = None
    phone: str | None = None

    for line in vcard.splitlines():
        line = line.strip()
        upper = line.upper()

        if upper.startswith("FN:") and not name:
            name = line[3:].strip() or None
            continue

        # Prefer waid (WhatsApp ID) — already normalized digits
        waid_match = re.search(r"waid=(\d+)", line)
        if waid_match and not phone:
            phone = waid_match.group(1)
            continue

        # Fallback: extract digits from TEL value
        if upper.startswith("TEL") and not phone:
            tel_value = line.split(":", 1)[-1].strip()
            digits = re.sub(r"\D", "", tel_value)
            if len(digits) >= 10:
                phone = digits

    return name, phone


def _extract_quoted_message_body(message: dict[str, Any], data: dict[str, Any]) -> str | None:
    context_info = (
        _nested_get(message, "extendedTextMessage", "contextInfo")
        or _nested_get(message, "contextInfo")
        or data.get("contextInfo")
    )
    if not isinstance(context_info, dict):
        return None
    quoted = context_info.get("quotedMessage")
    if not isinstance(quoted, dict):
        return None
    candidates = [
        quoted.get("conversation"),
        _nested_get(quoted, "extendedTextMessage", "text"),
        _nested_get(quoted, "textMessage", "text"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _detect_message_type(message: dict[str, Any], data: dict[str, Any], text: str | None) -> str:
    raw_type = str(data.get("messageType") or data.get("type") or "").lower()

    if "audio" in raw_type or "audioMessage" in message or "audio" in message:
        return "audio"

    if "contact" in raw_type or "contactMessage" in message or "contactsArrayMessage" in message:
        return "contact"

    if text:
        return "text"

    return raw_type or "unknown"


def _nested_get(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
