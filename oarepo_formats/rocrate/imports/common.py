# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""What the import route and its task need to know about a model's services.

Two questions, in one place so that the route and the task can not answer them differently:

- does the model have drafts? A record created by this import is a draft only then, and only then is there
  a `publish` to call;
- which files service do the imported files go to - the draft's or the record's?

The answer is taken from the service's *class*, not from the presence of a `publish` method: a service of
another kind may grow one (oarepo-requests adds methods to services), and the import would then take the
draft branch for a model without drafts.
"""

from __future__ import annotations

from typing import Any

from invenio_drafts_resources.services import RecordService
from oarepo_runtime.proxies import current_runtime


def has_drafts(service: Any) -> bool:
    """Return whether the model's record service is the drafts one."""
    return isinstance(service, RecordService)


def files_service_for(service: Any) -> Any:
    """Return the files service the imported files go to: the draft's, or the record's (None without files)."""
    model = current_runtime.get_model_for_record_class(service.record_cls)
    return getattr(model, "draft_file_service" if has_drafts(service) else "file_service", None)
