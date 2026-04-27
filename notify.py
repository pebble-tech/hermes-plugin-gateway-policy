"""Owner-notification helper.

Sends a one-off message via a Hermes gateway adapter. Used by the handover
flow to alert the owner when a customer triggers handover, and to confirm
takeback. Best-effort: a missing adapter or send failure logs and returns.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger("gateway-policy.notify")


def _strip_whatsapp_suffix(value: str) -> str:
    """Last-resort canonicalisation: strip ``+``, device tag, and JID/LID
    suffix to produce something close to a phone number. Lossy but safe."""
    return (
        str(value or "")
        .strip()
        .replace("+", "", 1)
        .split(":", 1)[0]
        .split("@", 1)[0]
    )


def format_chat_link(platform: str, chat_id: str) -> Tuple[str, str]:
    """Resolve ``(customer_phone, customer_link)`` for owner notifications.

    Per-platform behaviour:

    * ``whatsapp``: canonicalise via ``gateway.whatsapp_identity`` (with a
      pre-#15191 ``gateway.session`` fallback) so LIDs are walked back to
      a phone via ``$HERMES_HOME/whatsapp/session/lid-mapping-*.json``.
      Strip any remaining ``@s.whatsapp.net`` / ``@lid`` / device suffix
      before formatting a ``https://wa.me/<phone>`` link.
    * ``telegram``: chat_id is the numeric user_id; emit a
      ``tg://user?id=<id>`` deep link.
    * Anything else (discord, matrix, bluebubbles, api_server, …): we have
      no clean URL form, so return the raw chat_id for both fields.

    Always best-effort — never raises. Empty / falsy input returns
    ``("", "")`` so the caller can fold it into a format() call without
    needing to guard.
    """
    p = (platform or "").strip().lower()
    cid = str(chat_id or "").strip()
    if not cid:
        return "", ""

    if p == "whatsapp":
        resolved = cid
        try:
            from gateway.whatsapp_identity import canonical_whatsapp_identifier

            resolved = canonical_whatsapp_identifier(cid)
        except ImportError:
            try:
                from gateway.session import canonical_whatsapp_identifier

                resolved = canonical_whatsapp_identifier(cid)
            except ImportError:
                resolved = cid
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("whatsapp canonicalize failed: %s", exc)
            resolved = cid
        phone = _strip_whatsapp_suffix(resolved)
        if phone:
            return phone, f"https://wa.me/{phone}"
        return phone, cid

    if p == "telegram":
        return cid, f"tg://user?id={cid}"

    return cid, cid


def _resolve_platform(gateway: Any, platform: str):
    """Translate config-string platform to Hermes Platform enum."""
    try:
        from gateway.config import Platform
    except Exception as exc:
        logger.warning("Platform import failed: %s", exc)
        return None
    try:
        return Platform(platform.lower())
    except (KeyError, ValueError):
        logger.warning("unknown platform %r in owner config", platform)
        return None


async def _send_async(adapter: Any, chat_id: str, message: str) -> bool:
    try:
        await adapter.send(chat_id, message)
        return True
    except Exception as exc:
        logger.warning("owner notify send failed: %s", exc)
        return False


def notify_owner(gateway: Any, *, owner_platform: str, owner_chat_id: str, message: str) -> bool:
    """Send `message` to the configured owner chat. Synchronous interface;
    schedules the underlying coroutine on the gateway's loop when available.

    Returns True on schedule/send success, False otherwise.
    """
    if not owner_platform or not owner_chat_id or not message:
        return False

    platform_enum = _resolve_platform(gateway, owner_platform)
    if platform_enum is None:
        return False

    adapters = getattr(gateway, "adapters", None) or {}
    adapter = adapters.get(platform_enum)
    if adapter is None:
        logger.warning("no adapter loaded for owner platform %s", owner_platform)
        return False

    coro = _send_async(adapter, owner_chat_id, message)

    # Try to schedule on a running loop; fall back to a fresh one-off run.
    loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
        return True

    try:
        asyncio.run(coro)
        return True
    except Exception as exc:
        logger.warning("owner notify run failed: %s", exc)
        return False
