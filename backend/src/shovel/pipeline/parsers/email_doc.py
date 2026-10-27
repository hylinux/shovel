"""Parse normalized Outlook mail, never arbitrary .msg or .eml bytes."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from shovel.domain.enums import BlockKind, DocClass
from shovel.exceptions.pipeline import ParseError
from shovel.pipeline.parsers.base import Block, ParsedDocument, ParseHint, ParserSpec, clean_text
from shovel.services.outlook import OutlookMail


def _date(value: int | None) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else "(unknown)"


class EmailParser:
    spec = ParserSpec(
        name="outlook_mail", version="1", doc_classes=frozenset({DocClass.EMAIL}), suffixes=frozenset()
    )

    def parse(self, payload: bytes, hint: ParseHint) -> ParsedDocument:
        try:
            mail = OutlookMail.model_validate_json(payload)
        except ValidationError as exc:
            raise ParseError(hint.uri, "Invalid normalized Outlook mail record.") from exc
        if not mail.body.strip():
            raise ParseError(hint.uri, "Mail has no readable body.")
        title = mail.subject or "(No subject)"
        path = (title, f"From: {mail.sender}", f"Received: {_date(mail.received_at)}")
        headers = (
            f"Subject: {title}\nFrom: {mail.sender}\nTo: {mail.to}\nCc: {mail.cc}\n"
            f"Sent: {_date(mail.sent_at)}\nReceived: {_date(mail.received_at)}\nFolder: {mail.folder_path}"
        )
        text = f"{headers}\n\n{clean_text(mail.body)}"
        truncated = mail.truncated or len(text) > hint.max_chars
        text = text[: hint.max_chars]
        blocks = tuple(
            Block(text=paragraph, kind=BlockKind.PARAGRAPH, path=path, locator=f"mail paragraph {index}")
            for index, paragraph in enumerate(text.split("\n\n"), 1)
            if paragraph.strip()
        )
        return ParsedDocument(
            blocks=blocks,
            title=title,
            doc_class=DocClass.EMAIL,
            parser=self.spec.name,
            parser_version=self.spec.version,
            truncated=truncated,
            meta={
                "sender": mail.sender,
                "to": mail.to,
                "cc": mail.cc,
                "sent_at": mail.sent_at,
                "received_at": mail.received_at,
                "folder_path": mail.folder_path,
                "thread_id": mail.thread_id,
                "attachments": [item.model_dump() for item in mail.attachments],
            },
        )


PARSER = EmailParser()
