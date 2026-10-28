# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The `aip-access` action: who may create the Archival Information Package of a record.

It is an Invenio access action like `administration-access`, granted with the `invenio access` CLI:

```bash
invenio access allow aip-access user <email>
invenio access allow aip-access role <role>
```

and a permission generator [AIPAccess][oarepo_formats.rocrate.permissions.AIPAccess] for the model's
permission policy, so that a preservation officer can open a restricted record:

```python
class MyRecordPermissionPolicy(RDMRecordPermissionPolicy):
    can_read = [*RDMRecordPermissionPolicy.can_read, AIPAccess()]
    can_read_files = [*RDMRecordPermissionPolicy.can_read_files, AIPAccess()]
```

oarepo-formats never changes a model's policy: which records a holder may open is the deployment's
decision. The RO-Crate AIP export itself only checks the action (see
[oarepo_formats.rocrate.exports.aip][]); it does not grant access to a record the holder can not open.

Holders of `superuser-access` hold `aip-access` implicitly, because `invenio_access.permissions.Permission`
adds that need.
`administration-access` does *not* imply it: an administrator gets the BagIt export's size limit bypass,
a preservation officer gets the AIP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from invenio_access import action_factory
from invenio_access.permissions import Permission
from invenio_records_permissions.generators import Generator
from invenio_search.engine import dsl

if TYPE_CHECKING:
    import flask_principal

aip_access_action = action_factory("aip-access")
"""Invenio access action allowing the RO-Crate AIP export; registered under `invenio_access.actions`."""

aip_access_permission = Permission(aip_access_action)
"""Permission of the holders of `aip-access`.

A `Permission` caches the expansion of its action after the first check, so a long-lived instance does
not see a grant added later in the process; the export therefore checks
[has_aip_access][oarepo_formats.rocrate.permissions.has_aip_access], which builds a fresh one.
"""


def has_aip_access(identity: flask_principal.Identity | None) -> bool:
    """Return whether an identity holds `aip-access`, or `superuser-access` (which implies it).

    A fresh `Permission` is built per call: the expansion it caches is computed from the current grants,
    so `invenio access allow aip-access ...` takes effect without restarting the process.
    """
    return identity is not None and Permission(aip_access_action).allows(identity)


class AIPAccess(Generator):
    """Permission generator allowing the holders of `aip-access`, like `Administration()`.

    It can be added to a model's permission policy (`can_read`, `can_read_files`, ...). Its
    `query_filter` lets the holders find every record in searches, as `SystemProcess` does, so that a
    preservation officer does not have to know the id of a restricted record to export it.
    """

    def needs(self, **kwargs: Any) -> list[Any]:  # noqa: ARG002 the generator takes permission context
        """Return the needs the generator allows."""
        return [aip_access_action]

    def query_filter(
        self,
        identity: flask_principal.Identity | None = None,
        **kwargs: Any,  # noqa: ARG002 the permission context
    ) -> Any:
        """Return a filter that matches every record for a holder, `None` for everyone else."""
        if identity is not None and aip_access_action in identity.provides:
            return dsl.Q("match_all")
        return None
