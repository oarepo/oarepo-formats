# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of reading a detached RO-Crate Package: the root data entity, the file plan and hostile input."""

from __future__ import annotations

import json

import pytest
from marshmallow import ValidationError

from oarepo_formats.rocrate.imports.crate import CrateFile, ROCrate

PROFILE = "https://w3id.org/ro/crate/1.3"
FILES_URL = "https://source.example.org/api/datasets/abc-123/files"


def descriptor(about: str = "./", conforms_to: object = None, entity_id: str = "ro-crate-metadata.json") -> dict:
    return {
        "@id": entity_id,
        "@type": "CreativeWork",
        "about": {"@id": about},
        "conformsTo": conforms_to or {"@id": PROFILE},
    }


def file_entity(key: str, *, url: str | None = None, **extra: object) -> dict:
    entity = {"@id": url or f"{FILES_URL}/{key}/content", "@type": "File", "name": key}
    entity.update(extra)
    return entity


def document(*entities: dict, **root_properties: object) -> dict:
    """Return a crate with the given entities behind the root's `hasPart`."""
    root = {"@id": "./", "@type": "Dataset", "name": "A dataset", "hasPart": [{"@id": e["@id"]} for e in entities]}
    root.update(root_properties)
    return {"@context": "https://w3id.org/ro/crate/1.3/context", "@graph": [descriptor(), root, *entities]}


def reversed_graph(doc: dict) -> dict:
    doc["@graph"].reverse()  # the spec does not order @graph: the root is found through the descriptor
    return doc


def legacy_descriptor(doc: dict) -> dict:
    doc["@graph"][0] = descriptor(entity_id="ro-crate-metadata.jsonld")
    return doc


def conforms_to_array(doc: dict) -> dict:
    doc["@graph"][0]["conformsTo"] = [{"@id": PROFILE}, {"@id": "https://example.org/profiles/mine/1.0"}]
    return doc


def doi_root(doc: dict) -> dict:
    doc["@graph"][0]["about"] = {"@id": "https://doi.org/10.1/x"}
    doc["@graph"][1]["@id"] = "https://doi.org/10.1/x"
    return doc


@pytest.mark.parametrize(
    ("mutate", "root_id"),
    [
        pytest.param(lambda doc: doc, "./", id="descriptor-first"),
        pytest.param(reversed_graph, "./", id="not-by-position"),
        pytest.param(legacy_descriptor, "./", id="legacy-jsonld-descriptor"),
        pytest.param(conforms_to_array, "./", id="1.1-conforms-to-array"),
        pytest.param(doi_root, "https://doi.org/10.1/x", id="doi-root"),
    ],
)
def test_the_root_is_found_through_the_descriptor(mutate, root_id):
    crate = ROCrate(mutate(document(file_entity("data.csv"))))

    assert crate.root_id == root_id
    assert crate.name == "A dataset"
    assert crate.files == (CrateFile(key="data.csv", url=f"{FILES_URL}/data.csv/content"),)


NESTED = {"@id": "results/", "@type": "Dataset", "name": "results", "hasPart": [{"@id": "https://x.org/raw.csv"}]}
CYCLE_A = {"@id": "a/", "@type": "Dataset", "name": "a", "hasPart": [{"@id": "b/"}]}
CYCLE_B = {"@id": "b/", "@type": "Dataset", "name": "b", "hasPart": [{"@id": "a/"}, {"@id": "https://x.org/f.csv"}]}
NAMELESS = {"@id": "https://x.org/files/a%20b/table%20one.csv/content", "@type": "File"}
CONTENT_URL = {"@id": "#file-1", "@type": "File", "name": "data.csv", "contentUrl": "https://x.org/data.csv"}


@pytest.mark.parametrize(
    ("entities", "has_part", "files"),
    [
        pytest.param(
            [NESTED, file_entity("raw.csv", url="https://x.org/raw.csv")],
            ["results/"],
            [CrateFile(key="results/raw.csv", url="https://x.org/raw.csv")],
            id="nested-dataset-prefixes-the-key",
        ),
        pytest.param(
            [file_entity("data.csv", localPath="dir/other name.csv")],
            None,
            [CrateFile(key="dir/other name.csv", url=f"{FILES_URL}/data.csv/content")],
            id="local-path-wins-over-the-name",
        ),
        pytest.param(
            [NAMELESS],
            None,
            [CrateFile(key="table one.csv", url=NAMELESS["@id"])],
            id="key-from-the-url-before-content",
        ),
        pytest.param(
            [file_entity("data.csv", contentSize="1234", md5="9e107d9d372bb6826bd81d3542a419d6")],
            None,
            [
                CrateFile(
                    key="data.csv",
                    url=f"{FILES_URL}/data.csv/content",
                    size=1234,
                    checksum="md5:9e107d9d372bb6826bd81d3542a419d6",
                )
            ],
            id="size-and-checksum",
        ),
        pytest.param(
            [file_entity("data.csv"), file_entity("orphan.csv")],
            [f"{FILES_URL}/data.csv/content"],
            [CrateFile(key="data.csv", url=f"{FILES_URL}/data.csv/content")],
            id="unreachable-file-is-not-content",
        ),
        pytest.param(
            [CYCLE_A, CYCLE_B, file_entity("f.csv", url="https://x.org/f.csv")],
            ["a/"],
            [CrateFile(key="a/b/f.csv", url="https://x.org/f.csv")],
            id="directory-cycle",
        ),
        pytest.param(
            [{"@id": "#thing", "@type": "CreativeWork", "name": "A thing"}], None, [], id="neither-file-nor-dataset"
        ),
        pytest.param([CONTENT_URL], None, [CrateFile(key="data.csv", url="https://x.org/data.csv")], id="content-url"),
        pytest.param(
            [file_entity("data.csv", contentUrl="https://remote.org/data.csv")],
            None,
            [CrateFile(key="data.csv", url="https://remote.org/data.csv")],
            id="content-url-wins-over-the-id",
        ),
    ],
)
def test_the_file_plan(entities, has_part, files):
    doc = document(*entities)
    if has_part is not None:
        doc["@graph"][1]["hasPart"] = [{"@id": identifier} for identifier in has_part]

    assert ROCrate(doc).files == tuple(files)


def with_file(entity: dict) -> dict:
    return document(entity)


@pytest.mark.parametrize(
    ("doc", "match"),
    [
        pytest.param("not a document", "@graph", id="not-an-object"),
        pytest.param({"@graph": "nope"}, "@graph", id="graph-not-a-list"),
        pytest.param({"@graph": [{"@type": "Dataset"}]}, "string @id", id="no-id"),
        pytest.param({"@graph": [{"@id": "./", "@type": "Dataset"}]}, "conformsTo", id="no-descriptor"),
        pytest.param(
            {"@graph": [{**descriptor(), "about": None}, {"@id": "./", "@type": "Dataset"}]},
            "Unknown root",
            id="descriptor-without-about",
        ),
        pytest.param(
            {"@graph": [descriptor(about="./missing"), {"@id": "./", "@type": "Dataset"}]},
            "Unknown root",
            id="about-points-nowhere",
        ),
        pytest.param(
            {
                "@graph": [
                    descriptor(conforms_to={"@id": "https://example.org/other/1.0"}),
                    {"@id": "./", "@type": "Dataset"},
                ]
            },
            "conformsTo",
            id="foreign-conforms-to",
        ),
        pytest.param(
            {"@graph": [descriptor(), {"@id": "./", "@type": "CreativeWork"}]}, "not a Dataset", id="root-not-a-dataset"
        ),
        pytest.param(with_file({"@id": "#x", "@type": "File", "name": "x.csv"}), r"no absolute http\(s\)", id="no-url"),
        pytest.param(
            with_file(file_entity("x.csv", url="file:///etc/passwd")), r"no absolute http\(s\)", id="file-url"
        ),
        pytest.param(
            with_file(file_entity("x.csv", url="/api/files/x/content")), r"no absolute http\(s\)", id="relative-url"
        ),
        pytest.param(with_file(file_entity("../evil.csv")), "can not be used as a path", id="traversal"),
        pytest.param(with_file(file_entity("a/../../evil.csv")), "can not be used as a path", id="traversal-nested"),
        pytest.param(
            document(file_entity("data.csv"), file_entity("data.csv", url="https://x.org/other/content")),
            "Two files of the crate use the key",
            id="duplicate-key",
        ),
    ],
)
def test_an_unusable_crate_is_refused(doc, match):
    with pytest.raises(ValidationError, match=match):
        ROCrate(doc)


#
# references
#
def test_references_are_expanded_unless_they_point_outside_or_are_inline():
    author = {"@id": "https://orcid.org/0000-0001-2345-6789", "@type": "Person", "name": "Doe, Jane"}
    inline = {"@id": "#org", "name": "Inline name", "affiliation": [{"@id": "#parent"}]}
    doc = document(
        author,
        {"@id": "#org", "@type": "Organization", "name": "Graph name"},
        {"@id": "#parent", "@type": "Organization", "name": "Parent"},
        author=[{"@id": author["@id"]}],
        license={"@id": "https://creativecommons.org/licenses/by/4.0/"},
        publisher=inline,
    )

    root = ROCrate(doc).root

    assert root["author"] == [author]
    # a reference outside the graph stays a reference
    assert root["license"] == {"@id": "https://creativecommons.org/licenses/by/4.0/"}
    # only a pure reference is replaced: an inline entity keeps its own properties
    assert root["publisher"] == {
        "@id": "#org",
        "name": "Inline name",
        "affiliation": [{"@id": "#parent", "@type": "Organization", "name": "Parent"}],
    }


def ladder(levels: int) -> dict:
    """Return a crate whose references multiply: two entities per level, each referencing both of the next.

    Expanding such a graph without a depth limit is exponential (`2 ** levels` paths): a ~3 KB body of this
    shape would cost minutes of CPU.
    """
    entities = [
        {
            "@id": f"#level{level}-{branch}",
            "@type": "Organization",
            "name": f"Organization {level}-{branch}",
            "affiliation": [{"@id": f"#level{level + 1}-a"}, {"@id": f"#level{level + 1}-b"}],
        }
        for level in range(levels)
        for branch in ("a", "b")
    ]
    entities.extend(
        {"@id": f"#level{levels}-{branch}", "@type": "Organization", "name": "Bottom"} for branch in ("a", "b")
    )
    return document(*entities, author=[{"@id": "#level0-a"}, {"@id": "#level0-b"}])


def test_a_graph_whose_paths_multiply_is_expanded_in_bounded_size():
    """A posted crate can not cost exponential time: the expansion is capped in depth, so it stays linear."""
    doc = ladder(levels=30)

    crate = ROCrate(doc)

    assert len(json.dumps(crate.root)) < 12 * len(json.dumps(doc))
    level_2 = crate.root["author"][0]["affiliation"][0]["affiliation"][0]
    assert level_2["affiliation"] == [{"@id": "#level3-a"}, {"@id": "#level3-b"}]  # deeper stays a reference


def test_an_attached_crate_references_a_web_based_file_by_its_content_url():
    """The AIP's remote file: the source's `/content` as `@id`, the remote URL as `contentUrl`, no member."""
    stored = file_entity("data.csv", url="data/data.csv", contentUrl=f"{FILES_URL}/data.csv/content")
    remote = file_entity("big.nc", localPath="big.nc", contentUrl="https://remote.org/big.nc")

    crate = ROCrate(document(stored, remote), attached=True)

    assert crate.files == (
        CrateFile(key="data.csv", path="data/data.csv"),
        CrateFile(key="big.nc", url="https://remote.org/big.nc"),
    )
