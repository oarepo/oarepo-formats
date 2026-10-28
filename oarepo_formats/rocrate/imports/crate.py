# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Reading an RO-Crate - the import's view of a crate, detached (spec 4.4) or attached (spec 4.3).

The reader takes the crate's JSON-LD document and answers the two questions the import has: what does the
crate say about the record (the root data entity, with its references resolved), and which files does it
refer to - by which URL (detached) or archive member (attached) - under which key. It deliberately does
not know the model: the root entity is handed to
[ROCrateToDataCiteSchema][oarepo_formats.rocrate.imports.crate_datacite.ROCrateToDataCiteSchema], which
turns it into DataCite JSON.

## What is required, and what is not

The reader follows the specification where it is a *requirement*, and is lenient where the specification is
a recommendation, so that a crate produced by another tool imports as well as one produced by this package:

- the **RO-Crate Metadata Descriptor** (spec: "Root Data Entity"): an entity with `@id`
  `ro-crate-metadata.json` (legacy: `ro-crate-metadata.jsonld`) and `about` -> the root. The root is found
  with the specification's algorithm, never by its position in `@graph`;
- its `conformsTo` must name `https://w3id.org/ro/crate/` (any 1.x); in 1.1 and older it may be an array
  that also lists profiles, so a list is accepted;
- the root data entity's `@type` must contain `Dataset`;
- every file must be reachable from the root through `hasPart`, directly or through nested `Dataset`
  entities (spec: "Data Entities"); a `File` entity that is not reachable is not part of the crate and is
  ignored, and so are other entities (persons, organisations, licences, ...);
- in a detached package every data entity is web-based: its `@id` must be an absolute `http(s)` URL; in
  an attached one it is the member's path, relative to the crate root;
- a `File` the root also names in `subjectOf` is a metadata member of the package (the export's own
  README, `record.json`, ...), not a record file.

Everything the crate does not say is left out (a missing `name`, `contentSize`, checksum, ...), and the
model decides whether the result is acceptable - a record without a title fails its own validation with a
readable error.

## What the reader does *not* do

It does not download anything and does not expand the graph with JSON-LD processing: the document is the
flattened form this package's export produces and the specification describes (spec 4.2), where a reference
is `{"@id": ...}`. It reads the keys as the RO-Crate terms, accepting the prefixed and full IRIs and the
value objects other tools write for them, and refuses a crate whose inline `@context` gives a term it reads
another meaning (see [oarepo_formats.rocrate.imports.jsonld][]).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from marshmallow import ValidationError

from ...files import CHECKSUM_TERMS
from ..exports.zip import check_file_key
from .jsonld import as_list, check_context, literal, normalize, reference_id, types

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

METADATA_DESCRIPTOR = "ro-crate-metadata.json"
LEGACY_METADATA_DESCRIPTOR = "ro-crate-metadata.jsonld"

PROFILE_PREFIX = "https://w3id.org/ro/crate/"

FILE_TYPES = frozenset({"File", "MediaObject"})
"""`@type`s of a file: `File` is the RO-Crate alias of `schema:MediaObject`; a crate may use either."""

DATASET_TYPE = "Dataset"

MAX_REFERENCE_DEPTH = 3
"""How many reference levels below the root are expanded into entities.

The converter (and this package's export, whose inverse it is) looks two levels down: the root, an entity
it names (`author`, `funder`, `spatialCoverage`, ...), and what that entity names (`affiliation`, `geo`,
...). Expanding deeper is not worth its cost: on a graph whose paths multiply (a "diamond ladder", where
each level has two entities that both reference the two of the next level) the number of paths doubles per
level, so a 2 KB crate would cost minutes of CPU and an exponential amount of memory. A property that
would need a deeper entity keeps its `{"@id": ...}` reference.
"""


@dataclass(frozen=True)
class CrateFile:
    """One file of the crate, as the import has to add it."""

    key: str
    """The file key, a safe relative path (`check_file_key`)."""

    url: str | None = None
    """A referenced file - every file of a detached crate, a web-based one of an attached crate: where the
    content is - the entity's `contentUrl`, else its `@id`, an absolute `http(s)` URL."""

    path: str | None = None
    """A stored file of an attached crate: the member of the archive holding the content, relative to the
    crate root."""

    size: int | None = None
    """`contentSize`, if the crate states it."""

    checksum: str | None = None
    """The first checksum the crate states, in Invenio's `"<algorithm>:<hex>"` form (e.g. `"md5:abc..."`)."""

    checksums: Mapping[str, str] = field(default_factory=dict, compare=False)
    """Every checksum the crate states, by algorithm (`{"md5": "<hex>", "sha512": "<hex>"}`) - what the import
    verifies the bytes against. Not compared: two plans of the same file are equal whatever they state."""


class ROCrate:
    """A parsed and validated RO-Crate Package, detached or attached.

    `root` is the root data entity with the references below it expanded (see `MAX_REFERENCE_DEPTH`); the
    expanded dicts are shared between the properties that reach the same entity, so callers must treat
    them as read-only.

    A **detached** crate (spec 4.4) has web-based data entities: every file is fetched from its `@id`.
    An **attached** crate (spec 4.3) keeps its files in the package next to the metadata: every file is a
    member of the archive, named by its `@id` relative to the crate root, and `contentUrl` (if any) is
    ignored - it points at the repository the package came from.
    """

    def __init__(self, document: Any, *, attached: bool = False) -> None:
        """Parse and validate the document.

        Args:
            document: the JSON-LD document (`ro-crate-metadata.json`).
            attached: whether the files live in the package (`True`) or are web-based (`False`).

        Raises:
            ValidationError: if the document is not an RO-Crate: no `@graph`, no metadata descriptor, a
                descriptor without `about`, an unknown root data entity, a root that is not a `Dataset`, a
                foreign or missing `conformsTo`, a file without a usable URL (detached) or `@id`
                (attached), an unsafe or duplicate key.

        """
        if not isinstance(document, dict) or not isinstance(document.get("@graph"), list):
            raise ValidationError("The RO-Crate document must be an object with an @graph array")
        check_context(document.get("@context"))
        self.document = document
        self._entities: dict[str, dict] = {}
        for entity in document["@graph"]:
            if not isinstance(entity, dict) or not isinstance(entity.get("@id"), str):
                raise ValidationError("Every @graph entity must be an object with a string @id")
            # a duplicated @id is merged by the spec's own processing; the first one wins here
            self._entities.setdefault(entity["@id"], normalize(entity))

        self.attached = attached
        self.root_id = self._find_root()
        self._expanded: dict[tuple[str, int], dict] = {}
        self.root: dict = self._expand(self._entities[self.root_id], MAX_REFERENCE_DEPTH)
        if DATASET_TYPE not in types(self.root):
            raise ValidationError(f"The root data entity {self.root_id!r} is not a Dataset")
        self.files: tuple[CrateFile, ...] = tuple(self._collect_files())

    @property
    def name(self) -> str | None:
        """The crate's `name` - the title of the record, if the crate has one."""
        name = self.root.get("name")
        return name if isinstance(name, str) and name.strip() else None

    #
    # the spec's algorithm
    #
    def _find_root(self) -> str:
        """Return the `@id` of the root data entity (spec: "Finding the Root Data Entity").

        Raises:
            ValidationError: if the document has no metadata descriptor with an `about`, or the entity it
                points at is not in the graph.

        """
        root_id = self._descriptor_about(METADATA_DESCRIPTOR)
        legacy_root_id = self._descriptor_about(LEGACY_METADATA_DESCRIPTOR)
        self._check_profile(self._entities.get(METADATA_DESCRIPTOR) or self._entities.get(LEGACY_METADATA_DESCRIPTOR))
        for candidate in (root_id, legacy_root_id):
            if candidate is not None and candidate in self._entities:
                return candidate
        raise ValidationError(
            "Unknown root data entity: the crate's RO-Crate Metadata Descriptor (ro-crate-metadata.json) "
            "must have an 'about' that points at an entity of the @graph"
        )

    def _descriptor_about(self, descriptor_id: str) -> str | None:
        """Return the `@id` the metadata descriptor is about, if the document has that descriptor."""
        descriptor = self._entities.get(descriptor_id)
        if descriptor is None:
            return None
        about = descriptor.get("about")
        about_id = about.get("@id") if isinstance(about, dict) else None
        return about_id if isinstance(about_id, str) else None

    def _check_profile(self, descriptor: Mapping[str, Any] | None) -> None:
        """Refuse a crate that does not declare RO-Crate 1.x, accepting the 1.1 array form.

        Raises:
            ValidationError: if `conformsTo` is missing or names another specification.

        """
        declared = descriptor.get("conformsTo") if descriptor else None
        declared_ids = [value if isinstance(value, str) else reference_id(value) for value in as_list(declared)]
        if not any(identifier and identifier.startswith(PROFILE_PREFIX) for identifier in declared_ids):
            raise ValidationError(
                f"The RO-Crate Metadata Descriptor must declare conformsTo '{PROFILE_PREFIX}<version>'"
            )

    #
    # the file list
    #
    def _collect_files(self) -> Iterator[CrateFile]:
        """Yield the files reachable from the root, walking nested `Dataset`s, refusing an unsafe plan.

        A file the root also names in `subjectOf` is a *metadata member* - this package's export describes
        its own included exports (`README.generated.md`, `metadata/record.json`, ...) that way - and is not
        payload, so it is not imported as a record file.

        Raises:
            ValidationError: for a file without a usable URL or `@id`, an unsafe key or a duplicate key.

        """
        metadata_members = {reference_id(reference) for reference in as_list(self.root.get("subjectOf"))}
        visited: set[str] = set()
        keys: dict[str, str] = {}
        for crate_file, entity_id in self._walk(self._entities[self.root_id], "", visited):
            if entity_id in metadata_members:
                continue
            if crate_file.key in keys:
                raise ValidationError(
                    f"Two files of the crate use the key {crate_file.key!r} "
                    f"({keys[crate_file.key]!r} and {(crate_file.path or crate_file.url)!r})"
                )
            keys[crate_file.key] = crate_file.path or crate_file.url or ""
            yield crate_file

    def _walk(self, entity: Mapping[str, Any], prefix: str, visited: set[str]) -> Iterator[tuple[CrateFile, str]]:
        """Yield `(file, its @id)` below `entity`, descending into `Dataset`s, with `prefix` for the keys."""
        for reference in as_list(entity.get("hasPart")):
            target = self._entities.get(reference_id(reference))
            if target is None or target["@id"] in visited:  # a dangling reference, or a cycle
                continue
            visited.add(target["@id"])
            target_types = types(target)
            if target_types & FILE_TYPES:
                yield self._crate_file(target, prefix), target["@id"]
            elif DATASET_TYPE in target_types:
                yield from self._walk(target, _directory_prefix(target, prefix), visited)

    def _crate_file(self, entity: Mapping[str, Any], prefix: str) -> CrateFile:
        """Return one `File` entity as the import needs it.

        In an attached crate a `File` whose `@id` is an absolute `http(s)` URL is a web-based data entity
        (spec 7.4.3): it is referenced, not carried by the archive - this package's archives describe a
        remote file that way - and it is read like a file of a detached crate.

        Raises:
            ValidationError: if the entity has no usable URL (detached) or `@id` (attached), or its key is
                not a safe relative path.

        """
        url = None
        if self.attached and not _is_web_url(entity.get("@id")):
            path = _member_path(entity)
            if path is None:
                raise ValidationError(
                    f"File {entity['@id']!r} has no @id naming its member of the package "
                    "(an attached RO-Crate keeps its data entities next to the metadata)"
                )
            key = path.removeprefix("data/")
        else:
            path = None
            url = _file_url(entity)
            if url is None:
                raise ValidationError(
                    f"File {entity['@id']!r} has no absolute http(s) URL to fetch the content from "
                    "(a detached RO-Crate describes web-based data entities)"
                )
            key = _file_key(entity, prefix, url)
        try:
            check_file_key(key)
        except ValueError as e:
            raise ValidationError(str(e)) from e
        checksums = _checksums(entity)
        checksum = next((f"{algorithm}:{value}" for algorithm, value in checksums.items()), None)
        return CrateFile(key=key, path=path, url=url, size=_size(entity), checksum=checksum, checksums=checksums)

    #
    # JSON-LD references
    #
    def _expand(self, entity: Mapping[str, Any], depth: int) -> dict:
        """Return the entity with the references below it replaced by the entities they point at.

        At most `depth` reference levels are expanded and an entity already expanded at that depth is
        reused, so the number of expansions is bounded by `depth` times the number of entities: a graph
        whose paths multiply can not blow up, and the depth limit is what cuts cycles (see
        `MAX_REFERENCE_DEPTH`). Only a *pure* reference (`{"@id": ...}` with nothing else) is replaced; an
        inline entity keeps its own properties.

        A reference whose target is not in the graph stays a bare `{"@id": url}` - a licence that is only
        an IRI, for instance.
        """
        key = (entity["@id"], depth)
        if key not in self._expanded:
            self._expanded[key] = {
                name: value if name == "@id" else self._expand_value(value, depth) for name, value in entity.items()
            }
        return self._expanded[key]

    def _expand_value(self, value: Any, depth: int) -> Any:
        """Return one property value with the references below it expanded, as far as `depth` allows."""
        if isinstance(value, list):
            return [self._expand_value(item, depth) for item in value]
        if not isinstance(value, dict):
            return value
        entity_id = reference_id(value) if set(value) == {"@id"} else None
        if entity_id is not None and depth > 0 and entity_id in self._entities:
            return self._expand(self._entities[entity_id], depth - 1)
        return {name: self._expand_value(item, depth) for name, item in value.items()}


def _file_url(entity: Mapping[str, Any]) -> str | None:
    """Return the URL of a file's content: its `contentUrl`, else `@id`, else `url`, if it is http(s).

    `contentUrl` comes first: it is where the bytes are (schema.org), while the `@id` may be a download
    route - this package's AIP gives a remote file the source's `/content` link as `@id` and the remote
    URL as `contentUrl`.
    """
    for candidate in (entity.get("contentUrl"), entity.get("@id"), entity.get("url")):
        if _is_web_url(candidate):
            return candidate
    return None


def _is_web_url(value: Any) -> bool:
    """Return whether a value is an absolute `http(s)` URL."""
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _directory_prefix(entity: Mapping[str, Any], prefix: str) -> str:
    """Return the key prefix of a nested `Dataset`: its `localPath` or `name`, below `prefix`."""
    segment = entity.get("localPath") or entity.get("name")
    if not isinstance(segment, str) or not segment.strip("/"):
        return prefix
    return f"{prefix}{segment.strip('/')}/"


def _member_path(entity: Mapping[str, Any]) -> str | None:
    """Return the archive path of an attached file: its `@id`, percent-decoded, relative to the crate root.

    `attach()` writes the path as `quote("data/<key>")`; a foreign crate may write `./data/<key>` or a
    plain relative path. The result is checked by `check_file_key` where it is used.
    """
    entity_id = entity.get("@id")
    if not isinstance(entity_id, str) or not entity_id.strip("/."):
        return None
    return unquote(entity_id).removeprefix("./").strip("/")


def _file_key(entity: Mapping[str, Any], prefix: str, url: str) -> str:
    """Return the key of a file: its `localPath`, else `prefix` + `name`, else the last URL segment.

    The URL fallback drops a trailing `/content`: every Invenio file URL ends with it (the export's own
    `@id` is `<files url>/<key>/content`), so taking the last segment as it is would name every such file
    `"content"` and make the second one a duplicate key.

    The result still has to pass [`check_file_key`][oarepo_formats.rocrate.exports.zip.check_file_key].
    """
    local_path = entity.get("localPath")
    if isinstance(local_path, str) and local_path.strip("/"):
        return local_path.strip("/")
    name = entity.get("name")
    if isinstance(name, str) and name.strip("/"):
        return f"{prefix}{name.strip('/')}"
    path = urlparse(url).path.rstrip("/")
    if path.endswith("/content"):
        path = path.removesuffix("/content")
    return prefix + unquote(path.rsplit("/", 1)[-1])


def _size(entity: Mapping[str, Any]) -> int | None:
    """Return `contentSize` as an int, if the crate states it as one."""
    size = literal(entity.get("contentSize"))
    if isinstance(size, bool):
        return None
    if isinstance(size, int):
        return size
    if isinstance(size, str) and size.strip().isdigit():
        return int(size.strip())
    return None


def _checksums(entity: Mapping[str, Any]) -> dict[str, str]:
    """Return the checksums the entity states, by algorithm, in the order of `CHECKSUM_TERMS`.

    The algorithms are the terms of the Workflow Run RO-Crate vocabulary this package writes with the
    export ([CHECKSUM_TERMS][oarepo_formats.files.CHECKSUM_TERMS]); an unknown term is ignored.
    """
    checksums = {}
    for algorithm in CHECKSUM_TERMS:
        value = literal(entity.get(algorithm))
        if isinstance(value, str) and value.strip():
            checksums[algorithm] = value.strip().lower()
    return checksums
