"""Send Message Tool -- cross-channel messaging via platform APIs.

Sends a message to a user or channel on any connected messaging platform
(Telegram, Discord, Slack). Supports listing available targets and resolving
human-friendly channel names to IDs. Works in both CLI and gateway contexts.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\n\n"
        "IMPORTANT: When the user asks to send to a specific channel or person "
        "(not just a bare platform name), call send_message(action='list') FIRST to see "
        "available targets, then send to the correct one.\n"
        "If the user just says a platform name like 'send to telegram', send directly "
        "to the home channel without listing first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list"],
                "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms."
            },
            "target": {
                "type": "string",
                "description": "Delivery target. Format: 'platform' (uses home channel), 'platform:#channel-name', or 'platform:chat_id'. Examples: 'telegram', 'discord:#bot-home', 'slack:#engineering'"
            },
            "message": {
                "type": "string",
                "description": "The message text to send. When file_path is also provided, this becomes the file caption."
            },
            "file_path": {
                "type": "string",
                "description": "Local file path to send as a document attachment. The file must exist on disk. Can be combined with 'message' to add a caption."
            }
        },
        "required": []
    }
}


def send_message_tool(args, **kw):
    """Handle cross-channel send_message tool calls."""
    action = args.get("action", "send")

    if action == "list":
        return _handle_list()

    return _handle_send(args)


def _handle_list():
    """Return formatted list of available messaging targets."""
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as e:
        return json.dumps({"error": f"Failed to load channel directory: {e}"})


def _validate_file_path(file_path: str) -> str | None:
    """Validate and sanitize a file path. Returns error string or None if OK.

    Security checks:
    - Rejects '..' components in raw input
    - Resolves symlinks via os.path.realpath
    - Verifies file exists and is a regular file
    - Ensures resolved path is under a trusted base directory
    """
    if not file_path:
        return None
    # Reject path traversal — check raw input before normalization resolves it away
    if ".." in file_path.split("/") or ".." in file_path.split(os.sep):
        return "Path traversal detected: '..' components are not allowed"
    # Resolve symlinks to canonical path
    real_path = os.path.realpath(file_path)
    if not os.path.exists(real_path):
        return f"File not found: {file_path}"
    if not os.path.isfile(real_path):
        return f"Path is not a file: {file_path}"
    # Verify the resolved path is under a trusted directory
    from pathlib import Path
    trusted_dirs = [
        Path(os.path.realpath(os.path.expanduser("~/.hermes"))),
        Path(os.path.realpath("/tmp")),
        Path(os.path.realpath(os.path.expanduser("~/Documents"))),
    ]
    extra = os.getenv("HERMES_TRUSTED_DOCUMENT_DIRS", "")
    if extra:
        for d in extra.split(":"):
            d = d.strip()
            if d:
                trusted_dirs.append(Path(os.path.expanduser(d)))
    resolved = Path(real_path)
    if not any(resolved == t or t in resolved.parents for t in trusted_dirs):
        return f"File path outside trusted directories: {file_path}"
    return None


def _handle_send(args):
    """Send a message or file to a platform target."""
    target = args.get("target", "")
    message = args.get("message", "")
    file_path = args.get("file_path", "")

    if not target:
        return json.dumps({"error": "'target' is required when action='send'"})
    if not message and not file_path:
        return json.dumps({"error": "At least one of 'message' or 'file_path' is required when action='send'"})

    # Validate file path if provided
    if file_path:
        file_error = _validate_file_path(file_path)
        if file_error:
            return json.dumps({"error": file_error})

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    chat_id = parts[1].strip() if len(parts) > 1 else None

    # Resolve human-friendly channel names to numeric IDs
    if chat_id and not chat_id.lstrip("-").isdigit():
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_name, chat_id)
            if resolved:
                chat_id = resolved
            else:
                return json.dumps({
                    "error": f"Could not resolve '{chat_id}' on {platform_name}. "
                    f"Use send_message(action='list') to see available targets."
                })
        except Exception:
            return json.dumps({
                "error": f"Could not resolve '{chat_id}' on {platform_name}. "
                f"Try using a numeric channel ID instead."
            })

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return json.dumps({"error": "Interrupted"})

    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
    except Exception as e:
        return json.dumps({"error": f"Failed to load gateway config: {e}"})

    platform_map = {
        "telegram": Platform.TELEGRAM,
        "discord": Platform.DISCORD,
        "slack": Platform.SLACK,
        "whatsapp": Platform.WHATSAPP,
    }
    platform = platform_map.get(platform_name)
    if not platform:
        avail = ", ".join(platform_map.keys())
        return json.dumps({"error": f"Unknown platform: {platform_name}. Available: {avail}"})

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        return json.dumps({"error": f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/gateway.json or environment variables."})

    used_home_channel = False
    if not chat_id:
        home = config.get_home_channel(platform)
        if home:
            chat_id = home.chat_id
            used_home_channel = True
        else:
            return json.dumps({
                "error": f"No home channel set for {platform_name} to determine where to send the message. "
                f"Either specify a channel directly with '{platform_name}:CHANNEL_NAME', "
                f"or set a home channel via: hermes config set {platform_name.upper()}_HOME_CHANNEL <channel_id>"
            })

    try:
        from model_tools import _run_async
        result = _run_async(_send_to_platform(platform, pconfig, chat_id, message, file_path=file_path or None))
        if used_home_channel and isinstance(result, dict) and result.get("success"):
            result["note"] = f"Sent to {platform_name} home channel (chat_id: {chat_id})"

        # Mirror the sent message into the target's gateway session
        if isinstance(result, dict) and result.get("success"):
            try:
                from gateway.mirror import mirror_to_session
                mirror_text = message or f"[Document: {os.path.basename(file_path)}]" if file_path else message
                source_label = os.getenv("HERMES_SESSION_PLATFORM", "cli")
                if mirror_to_session(platform_name, chat_id, mirror_text, source_label=source_label):
                    result["mirrored"] = True
            except Exception:
                pass

        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"Send failed: {e}"})


async def _send_to_platform(platform, pconfig, chat_id, message, file_path=None):
    """Route a message or file to the appropriate platform sender."""
    from gateway.config import Platform
    if platform == Platform.TELEGRAM:
        return await _send_telegram(pconfig.token, chat_id, message, file_path=file_path)
    elif platform == Platform.DISCORD:
        return await _send_discord(pconfig.token, chat_id, message, file_path=file_path)
    elif platform == Platform.SLACK:
        return await _send_slack(pconfig.token, chat_id, message, file_path=file_path)
    return {"error": f"Direct sending not yet implemented for {platform.value}"}


async def _send_telegram(token, chat_id, message, file_path=None):
    """Send via Telegram Bot API (one-shot, no polling needed)."""
    try:
        from telegram import Bot
        from pathlib import Path
        bot = Bot(token=token)

        if file_path:
            with open(file_path, "rb") as f:
                msg = await bot.send_document(
                    chat_id=int(chat_id),
                    document=f,
                    filename=Path(file_path).name,
                    caption=message[:1024] if message else None,
                )
            return {
                "success": True, "platform": "telegram", "chat_id": chat_id,
                "message_id": str(msg.message_id), "type": "document",
                "filename": Path(file_path).name,
            }
        else:
            msg = await bot.send_message(chat_id=int(chat_id), text=message)
            return {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": str(msg.message_id)}
    except ImportError:
        return {"error": "python-telegram-bot not installed. Run: pip install python-telegram-bot"}
    except Exception as e:
        return {"error": f"Telegram send failed: {e}"}


async def _send_discord(token, chat_id, message, file_path=None):
    """Send via Discord REST API (no websocket client needed)."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        url = f"https://discord.com/api/v10/channels/{chat_id}/messages"
        headers = {"Authorization": f"Bot {token}"}

        if file_path:
            from pathlib import Path
            # Read file into bytes to avoid leaking file descriptors via FormData
            file_bytes = open(file_path, "rb").read()
            filename = Path(file_path).name
            form = aiohttp.FormData()
            form.add_field("content", message or "")
            form.add_field(
                "files[0]",
                file_bytes,
                filename=filename,
            )
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=form) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        return {"error": f"Discord API error ({resp.status}): {body}"}
                    data = await resp.json()
                    return {
                        "success": True, "platform": "discord", "chat_id": chat_id,
                        "message_id": data.get("id"), "type": "document",
                        "filename": filename,
                    }
        else:
            headers["Content-Type"] = "application/json"
            chunks = [message[i:i+2000] for i in range(0, len(message), 2000)]
            message_ids = []
            async with aiohttp.ClientSession() as session:
                for chunk in chunks:
                    async with session.post(url, headers=headers, json={"content": chunk}) as resp:
                        if resp.status not in (200, 201):
                            body = await resp.text()
                            return {"error": f"Discord API error ({resp.status}): {body}"}
                        data = await resp.json()
                        message_ids.append(data.get("id"))
            return {"success": True, "platform": "discord", "chat_id": chat_id, "message_ids": message_ids}
    except Exception as e:
        return {"error": f"Discord send failed: {e}"}


async def _send_slack(token, chat_id, message, file_path=None):
    """Send via Slack Web API."""
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        if file_path:
            from pathlib import Path
            # Step 1: Get upload URL
            headers = {"Authorization": f"Bearer {token}"}
            filename = Path(file_path).name
            file_size = os.path.getsize(file_path)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://slack.com/api/files.getUploadURLExternal",
                    headers=headers,
                    params={"filename": filename, "length": str(file_size)},
                ) as resp:
                    url_data = await resp.json()
                    if not url_data.get("ok"):
                        return {"error": f"Slack upload URL failed: {url_data.get('error', 'unknown')}"}

                upload_url = url_data["upload_url"]
                file_id = url_data["file_id"]

                # Step 2: Upload file content
                with open(file_path, "rb") as f:
                    async with session.post(upload_url, data=f) as resp:
                        if resp.status not in (200, 201):
                            return {"error": f"Slack file upload failed ({resp.status})"}

                # Step 3: Complete upload and share to channel
                async with session.post(
                    "https://slack.com/api/files.completeUploadExternal",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "files": [{"id": file_id, "title": filename}],
                        "channel_id": chat_id,
                        "initial_comment": message or "",
                    },
                ) as resp:
                    complete_data = await resp.json()
                    if complete_data.get("ok"):
                        return {
                            "success": True, "platform": "slack", "chat_id": chat_id,
                            "file_id": file_id, "type": "document", "filename": filename,
                        }
                    return {"error": f"Slack complete upload failed: {complete_data.get('error', 'unknown')}"}
        else:
            url = "https://slack.com/api/chat.postMessage"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json={"channel": chat_id, "text": message}) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return {"success": True, "platform": "slack", "chat_id": chat_id, "message_id": data.get("ts")}
                    return {"error": f"Slack API error: {data.get('error', 'unknown')}"}
    except Exception as e:
        return {"error": f"Slack send failed: {e}"}


def _check_send_message():
    """Gate send_message on gateway running (always available on messaging platforms)."""
    platform = os.getenv("HERMES_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False


# --- Registry ---
from tools.registry import registry

registry.register(
    name="send_message",
    toolset="messaging",
    schema=SEND_MESSAGE_SCHEMA,
    handler=send_message_tool,
    check_fn=_check_send_message,
)
