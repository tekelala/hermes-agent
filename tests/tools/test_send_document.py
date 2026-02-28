"""Tests for document sending functionality across all layers."""

import json
import os
import sys
import importlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _import_send_message_tool():
    """Import send_message_tool directly, bypassing tools/__init__.py (needs firecrawl)."""
    spec = importlib.util.spec_from_file_location(
        "send_message_tool",
        PROJECT_ROOT / "tools" / "send_message_tool.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Pre-populate the tools.registry dependency so the registry import inside the
    # module works without importing tools/__init__.py.
    if "tools.registry" not in sys.modules:
        reg_spec = importlib.util.spec_from_file_location(
            "tools.registry",
            PROJECT_ROOT / "tools" / "registry.py",
        )
        reg_mod = importlib.util.module_from_spec(reg_spec)
        sys.modules["tools.registry"] = reg_mod
        reg_spec.loader.exec_module(reg_mod)
    # Pre-populate tools.interrupt
    if "tools.interrupt" not in sys.modules:
        int_spec = importlib.util.spec_from_file_location(
            "tools.interrupt",
            PROJECT_ROOT / "tools" / "interrupt.py",
        )
        int_mod = importlib.util.module_from_spec(int_spec)
        sys.modules["tools.interrupt"] = int_mod
        int_spec.loader.exec_module(int_mod)
    spec.loader.exec_module(mod)
    return mod


_smt = _import_send_message_tool()
SEND_MESSAGE_SCHEMA = _smt.SEND_MESSAGE_SCHEMA
_validate_file_path = _smt._validate_file_path
send_message_tool = _smt.send_message_tool


# ── Schema Tests ──────────────────────────────────────────────────────────

class TestSendMessageSchema:
    """Verify the send_message tool schema includes file_path."""

    def test_schema_has_file_path(self):
        """Schema properties must contain file_path with type string."""
        props = SEND_MESSAGE_SCHEMA["parameters"]["properties"]
        assert "file_path" in props
        assert props["file_path"]["type"] == "string"

    def test_schema_file_path_description(self):
        """file_path description must mention document or file."""
        desc = SEND_MESSAGE_SCHEMA["parameters"]["properties"]["file_path"]["description"]
        assert "document" in desc.lower() or "file" in desc.lower()


# ── Validation Tests ─────────────────────────────────────────────────────

class TestFilePathValidation:
    """Test _validate_file_path security and correctness."""

    def test_validate_none_path(self):
        """Empty or None file_path returns no error (valid — no file requested)."""
        assert _validate_file_path("") is None
        assert _validate_file_path(None) is None

    def test_validate_path_traversal_rejected(self):
        """Paths containing '..' components must be rejected."""
        result = _validate_file_path("/tmp/../etc/passwd")
        assert result is not None
        assert "traversal" in result.lower()

    def test_validate_nonexistent_file(self):
        """Non-existent file must return a 'not found' error."""
        result = _validate_file_path("/tmp/nonexistent_file_abc123.pdf")
        assert result is not None
        assert "not found" in result.lower()

    def test_validate_directory_rejected(self):
        """Directories must be rejected — only regular files allowed."""
        result = _validate_file_path("/tmp")
        assert result is not None
        assert "not a file" in result.lower()

    def test_validate_existing_file_ok(self):
        """A valid file under a trusted dir (/tmp) must pass validation."""
        # /tmp is in trusted dirs by default
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, dir="/tmp") as f:
            f.write(b"test content")
            f.flush()
            try:
                result = _validate_file_path(f.name)
                assert result is None  # No error
            finally:
                os.unlink(f.name)

    def test_validate_file_outside_trusted_dirs(self):
        """Files outside trusted directories must be rejected."""
        test_dir = tempfile.mkdtemp(prefix="untrusted_")
        test_file = os.path.join(test_dir, "secret.txt")
        try:
            with open(test_file, "w") as f:
                f.write("secret data")
            import shutil
            untrusted_dir = os.path.join(os.path.expanduser("~"), ".untrusted_test_dir")
            os.makedirs(untrusted_dir, exist_ok=True)
            untrusted_file = os.path.join(untrusted_dir, "secret.txt")
            with open(untrusted_file, "w") as f:
                f.write("secret")
            result = _validate_file_path(untrusted_file)
            assert result is not None
            assert "trusted" in result.lower()
            os.unlink(untrusted_file)
            os.rmdir(untrusted_dir)
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


# ── send_message_tool Integration Tests ──────────────────────────────────

class TestSendMessageToolWithFile:
    """Test send_message tool handler with file_path."""

    def test_send_no_target_returns_error(self):
        """Sending a file without a target must return an error."""
        result = json.loads(send_message_tool({"action": "send", "file_path": "/tmp/test.pdf"}))
        assert "error" in result

    def test_send_no_message_no_file_returns_error(self):
        """Sending with neither message nor file_path must return an error."""
        result = json.loads(send_message_tool({"action": "send", "target": "telegram:123"}))
        assert "error" in result

    def test_send_file_not_found_returns_error(self):
        """Sending a non-existent file must return a 'not found' error."""
        result = json.loads(send_message_tool({
            "action": "send",
            "target": "telegram:123",
            "file_path": "/nonexistent/file.pdf"
        }))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_send_file_path_traversal_returns_error(self):
        """Path traversal attempts must be caught and rejected."""
        result = json.loads(send_message_tool({
            "action": "send",
            "target": "telegram:123",
            "file_path": "/tmp/../etc/passwd"
        }))
        assert "error" in result
        assert "traversal" in result["error"].lower()


# ── extract_documents Tests ──────────────────────────────────────────────

class TestExtractDocuments:
    """Test DOCUMENT: tag extraction from response text."""

    def test_extract_single_document(self):
        """A single DOCUMENT: tag must be extracted with path and empty caption."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "Here is the file:\nDOCUMENT:/tmp/report.pdf\nDone."
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert docs[0][0] == "/tmp/report.pdf"
        assert docs[0][1] == ""
        assert "DOCUMENT:" not in cleaned
        assert "Done." in cleaned

    def test_extract_document_with_caption(self):
        """DOCUMENT: tag with pipe-separated caption must extract both."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "DOCUMENT:/tmp/report.pdf|Monthly revenue report"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert docs[0][0] == "/tmp/report.pdf"
        assert docs[0][1] == "Monthly revenue report"

    def test_extract_multiple_documents(self):
        """Multiple DOCUMENT: tags must all be extracted."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "Files:\nDOCUMENT:/tmp/a.pdf\nDOCUMENT:/tmp/b.xlsx\n"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 2
        assert docs[0][0] == "/tmp/a.pdf"
        assert docs[1][0] == "/tmp/b.xlsx"

    def test_extract_no_documents(self):
        """Content without DOCUMENT: tags must return empty list and unchanged text."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "No documents here, just text."
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 0
        assert cleaned == content

    def test_extract_documents_cleans_blank_lines(self):
        """Extracted tags must not leave triple-or-more blank lines."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "Before\n\n\nDOCUMENT:/tmp/file.pdf\n\n\n\nAfter"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert "\n\n\n" not in cleaned

    def test_extract_document_path_with_spaces(self):
        """Paths containing spaces must be fully captured up to the pipe."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "DOCUMENT:/tmp/my report.pdf|Quarterly report"
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert docs[0][0] == "/tmp/my report.pdf"
        assert docs[0][1] == "Quarterly report"
        assert "DOCUMENT:" not in cleaned

    def test_extract_document_path_with_spaces_no_caption(self):
        """Paths with spaces and no caption must capture the full path."""
        from gateway.platforms.base import BasePlatformAdapter
        content = "Here:\nDOCUMENT:/tmp/my report.pdf\nDone."
        docs, cleaned = BasePlatformAdapter.extract_documents(content)
        assert len(docs) == 1
        assert docs[0][0] == "/tmp/my report.pdf"
        assert docs[0][1] == ""


# ── Trusted Document Path Tests ───────────────────────────────────────────

class TestTrustedDocumentPath:
    """Test the _is_trusted_document_path security function."""

    def test_tmp_is_trusted(self):
        """Files under /tmp must be considered trusted."""
        from gateway.platforms.base import _is_trusted_document_path
        assert _is_trusted_document_path("/tmp/test.pdf") is True

    def test_hermes_dir_is_trusted(self):
        """Files under ~/.hermes must be considered trusted."""
        from gateway.platforms.base import _is_trusted_document_path
        hermes_path = os.path.expanduser("~/.hermes/document_cache/test.pdf")
        assert _is_trusted_document_path(hermes_path) is True

    def test_etc_passwd_is_not_trusted(self):
        """System files like /etc/passwd must be rejected."""
        from gateway.platforms.base import _is_trusted_document_path
        assert _is_trusted_document_path("/etc/passwd") is False

    def test_root_file_is_not_trusted(self):
        """Files at filesystem root must be rejected."""
        from gateway.platforms.base import _is_trusted_document_path
        assert _is_trusted_document_path("/secret.key") is False


# ── BasePlatformAdapter.send_document Fallback Test ──────────────────────

class TestBaseSendDocumentFallback:
    """Test the base class fallback for send_document."""

    @pytest.mark.asyncio
    async def test_fallback_sends_text(self):
        """Base send_document must fall back to text message with file path and caption."""
        from gateway.platforms.base import BasePlatformAdapter, SendResult
        from gateway.config import Platform, PlatformConfig

        class StubAdapter(BasePlatformAdapter):
            """Minimal stub for testing base class behavior."""

            async def connect(self):
                """Connect stub."""
                return True

            async def disconnect(self):
                """Disconnect stub."""
                pass

            async def send(self, chat_id, content, reply_to=None, metadata=None):
                """Capture sent content for assertion."""
                self._last_sent = content
                return SendResult(success=True, message_id="1")

            async def get_chat_info(self, chat_id):
                """Return minimal chat info."""
                return {"name": "test", "type": "dm"}

        config = PlatformConfig(token="test", enabled=True)
        adapter = StubAdapter(config, Platform.TELEGRAM)
        result = await adapter.send_document("123", "/tmp/test.pdf", caption="Here you go")
        assert result.success
        assert "📎" in adapter._last_sent
        assert "/tmp/test.pdf" in adapter._last_sent
        assert "Here you go" in adapter._last_sent


# ── TelegramAdapter.send_document Override Test ──────────────────────────

class TestTelegramSendDocument:
    """Test TelegramAdapter.send_document sends via Bot API."""

    @pytest.mark.asyncio
    async def test_send_document_calls_bot(self):
        """send_document must call bot.send_document with correct chat_id, filename, and caption."""
        from gateway.platforms.telegram import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(token="test-token", enabled=True)
        adapter = TelegramAdapter(config)

        # Mock the bot
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        mock_bot = AsyncMock()
        mock_bot.send_document = AsyncMock(return_value=mock_msg)
        adapter._bot = mock_bot

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            f.flush()
            try:
                result = await adapter.send_document("12345", f.name, caption="Test doc")
                assert result.success
                assert result.message_id == "42"
                mock_bot.send_document.assert_called_once()
                call_kwargs = mock_bot.send_document.call_args
                assert call_kwargs.kwargs["chat_id"] == 12345
                assert call_kwargs.kwargs["caption"] == "Test doc"
                assert call_kwargs.kwargs["filename"] == os.path.basename(f.name)
            finally:
                os.unlink(f.name)

    @pytest.mark.asyncio
    async def test_send_document_file_not_found(self):
        """send_document with non-existent file must return failure with error message."""
        from gateway.platforms.telegram import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(token="test-token", enabled=True)
        adapter = TelegramAdapter(config)
        adapter._bot = AsyncMock()

        result = await adapter.send_document("12345", "/nonexistent/file.pdf")
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_document_not_connected(self):
        """send_document without a connected bot must return 'not connected' error."""
        from gateway.platforms.telegram import TelegramAdapter
        from gateway.config import PlatformConfig

        config = PlatformConfig(token="test-token", enabled=True)
        adapter = TelegramAdapter(config)
        # _bot is None (not connected)

        result = await adapter.send_document("12345", "/tmp/test.pdf")
        assert not result.success
        assert "not connected" in result.error.lower()
