# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The mails of the RO-Crate import: three invenio-notifications builders and their templates.

An import whose files arrive after the response (fetched from their URLs, or written from a package
behind a URL) is asynchronous, so its outcome reaches the uploader by mail:

| `type` | Sent when |
|---|---|
| `rocrate-import.failed` | a file or the archive failed, the wait ran out, the draft changed, the publish was refused |
| `rocrate-import.completed` | every file arrived and no publish was requested |
| `rocrate-import.published` | the deferred publish committed |

The failure mail is always sent - nobody else learns about an asynchronous failure; the two "ok" ones are
switched off by `?notifications=off`.

The record is carried as a **snapshot** (`id`, `title`, `self_html`, `is_published`, `is_draft`), not as a
reference: records are resolved only for RDM's own service, and a snapshot keeps the mail correct if the
record changes afterwards. The templates live in `templates/semantic-ui/invenio_notifications/` and are
rendered by the e-mail backend in the recipient's locale, so `_()` inside them is translated per user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from invenio_notifications.models import Notification
from invenio_notifications.services.builders import NotificationBuilder
from invenio_notifications.services.generators import EntityResolve, UserEmailBackend
from invenio_users_resources.notifications.filters import UserPreferencesRecipientFilter
from invenio_users_resources.notifications.generators import UserRecipient

if TYPE_CHECKING:
    from collections.abc import Iterable


MAX_ERROR_LENGTH = 500
"""Longest error text a mail carries.

An exception's `str()` can be tens of kilobytes; an HTTP error is already one line.
"""


def snapshot(item: Any, title: str | None = None) -> dict:
    """Return the part of a result item the mails show.

    Args:
        item: a result item, the dict it serializes to, or None when there is nothing to point at.
        title: the title to show - the crate's `name`, because the record's title field is model-specific.

    """
    data = item.to_dict() if hasattr(item, "to_dict") else (item or {})
    links = data.get("links") or {}
    return {
        "id": data.get("id"),
        "title": title or (data.get("metadata") or {}).get("title"),
        "self_html": links.get("self_html"),
        "is_published": data.get("is_published"),
        "is_draft": data.get("is_draft"),
    }


def _truncated(value: Any) -> str | None:
    """Return an error text, cut to `MAX_ERROR_LENGTH`; None stays None."""
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= MAX_ERROR_LENGTH else f"{text[:MAX_ERROR_LENGTH]}..."


class ROCrateImportNotificationBuilder(NotificationBuilder):
    """Base of the import's mails: one user recipient, one e-mail, the user's own preference filter.

    The context carries only serializable values: it travels through celery to the sender, and a
    `LazyString` (a translatable message) can not be serialized. The words of a failure reason are
    therefore chosen by the templates, from the `reason` code.
    """

    @classmethod
    def build(  # ty: ignore[invalid-method-override] # the base takes **kwargs; this builder names them
        cls,
        user_id: str | int,
        record: dict,
        files: Iterable[dict] = (),
        reason: str | None = None,
        error: str | None = None,
    ) -> Notification:
        """Build the notification for one import outcome."""
        return Notification(
            type=cls.type,
            context={
                "receiver": {"user": str(user_id)},
                "record": record,
                "files": [{**file, "error": _truncated(file.get("error"))} for file in files],
                "reason": reason,
                "error": _truncated(error),
            },
        )

    # `ClassVar` would silence RUF012, but ty rejects a class variable overriding the base's instance
    # attribute (`NotificationBuilder.context = []`), so the annotation stays off (noqa on each line)
    context = [EntityResolve(key="receiver")]  # noqa: RUF012
    recipients = [UserRecipient(key="receiver")]  # noqa: RUF012
    recipient_filters = [UserPreferencesRecipientFilter()]  # noqa: RUF012
    recipient_backends = [UserEmailBackend()]  # noqa: RUF012


class ImportFailed(ROCrateImportNotificationBuilder):
    """A file failed, the wait ran out, the draft changed, or the deferred publish was refused."""

    type = "rocrate-import.failed"


class ImportCompleted(ROCrateImportNotificationBuilder):
    """Every file was fetched and no publish was requested."""

    type = "rocrate-import.completed"


class ImportPublished(ROCrateImportNotificationBuilder):
    """The deferred publish committed."""

    type = "rocrate-import.published"


BUILDERS = {builder.type: builder for builder in (ImportFailed, ImportCompleted, ImportPublished)}
"""The builders the extension puts into `NOTIFICATIONS_BUILDERS`."""
