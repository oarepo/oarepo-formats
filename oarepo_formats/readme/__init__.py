# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""README export of oarepo records.

Adds an export that describes a record as a README file in Markdown, following the
[README template](https://github.com/Czech-Data-Steward-Community/README_FILES_EXAMPLES/blob/main/templates/README-MD.md)
of the Czech Data Steward Community. It is rendered from the record's DataCite metadata and its files
by a Jinja template, which a repository can override.

To use the export, import the customization `AddReadmeExport` from `oarepo_formats.readme`.

## Requirements

The README is rendered from DataCite, so the model must have a DataCite JSON export registered under
the mimetype `application/vnd.datacite.datacite+json` - the same requirement as for the RO-Crate
export, see [oarepo_formats.rocrate][]. Without it, the README export fails with
`ValueError("No export found ...")`.

## Usage

Add [AddReadmeExport][oarepo_formats.readme.model.AddReadmeExport] to the model's customizations (in
`model.py`):

```python
from oarepo_formats.readme import (
    AddReadmeExport,
)

customizations = [
    AddReadmeExport(),
]
```

The export (code `readme`) is then offered on the landing page and served by the REST API for
`text/markdown`:

```bash
curl -H 'Accept: text/markdown' https://<repository>/api/<model>/<id>
```

So that readers know the README was not written by the authors of the dataset, it is downloaded as
`<id>.generated.md` and starts with a notice that it was generated automatically (the
`generated_notice` block of the template). If the model also has the RO-Crate ZIP export, the README is added to
the archive as `README.generated.md`, one of its included exports (see [oarepo_formats.rocrate.exports.zip][]).

## The README

The sections of the template are filled from DataCite and the record; a section without metadata is
left out, so that the README contains no instructions for the dataset author:

| Section | Source |
|---|---|
| Dataset name | the main title |
| Basic information | creators (with ORCID and affiliations), the `ContactPerson` contributors, rights, version, PID |
| Description | the `Abstract`, `SeriesInformation` descriptions, awards, related publications (`IsCitedBy`, ...) |
| Dataset contents | the files the user may download, with sizes and formats; the `TableOfContents` description |
| Data structure | the `TechnicalInfo` description |
| Methodology-provenance | `Collected` dates, places, `IsDerivedFrom` relations, the `Methods` description |
| Data processing | `IsCompiledBy` relations |
| Reuse | rights, `Requires` relations |
| Proposed citation | creators, publication year, title, publisher, resource type, PID, version |
| Acknowledgements | funding references, contributors with their roles, the credit of the template |
| Change history | the date the README was generated |

DataCite has no place for a variable description, data encoding, missing-value codes, data quality or
ethics, so these sections appear only if a custom template adds them.

## Changing the README

The export renders `oarepo_formats/readme/README.md.jinja`, which only extends
`oarepo_formats/readme/base.md.jinja` - the template with the content. Templates are looked up in the
instance's `templates` folder first, then in the packages, and with `APP_THEME = ["my-theme", ...]`
under each theme prefix first - Invenio's usual override mechanism. To change the README of the
repository, create one of

```
<instance>/templates/oarepo_formats/readme/README.md.jinja
<instance>/templates/my-theme/oarepo_formats/readme/README.md.jinja
```

extending `base.md.jinja` and replacing only the blocks to change. Every section has a block, and
every piece of information in it has a block of its own, nested in the section's block:

| Section block | Blocks of its pieces |
|---|---|
| `title` | |
| `generated_notice` | |
| `basic_information` | `authors`, `contacts`, `dataset_license`, `dataset_version`, `readme_version`, `pid` |
| `description_section` | `brief_description`, `research_context`, `related_publications` |
| `contents_section` | `directory_structure`, `file_descriptions` |
| `data_structure_section` | `technical_info` |
| `methodology_section` | `collection_dates`, `collection_location`, `original_data`, `collection_method` |
| `processing_section` | `processing_software` |
| `reuse_section` | `license_terms`, `requirements` |
| `citation_section` | `citation` |
| `acknowledgements_section` | `funding`, `contributors`, `template_credit` |
| `change_history_section` | `change_history` |

Replacing a piece keeps the rest of its section; replacing a section replaces its pieces as well. For
example, the authors on one line, and a request for the variables, which DataCite can not hold:

```jinja
{% extends "oarepo_formats/readme/base.md.jinja" %}

{% block authors %}
Authors: {{ creators | map(attribute="name") | join("; ") }}
{% endblock %}

{% block data_structure_section %}
## DATA STRUCTURE

> Describe the variables of the data: name, type, unit and meaning.
{% endblock %}
```

A section is shown only if its metadata is present, so to add content to a section DataCite leaves
empty, replace the whole section block, as above.

In the blocks, besides the variables of
[ReadmeSerializer.context][oarepo_formats.readme.serializer.ReadmeSerializer.context], the values
the default template computes are available - `title`, `creators`, `contacts`, `contributors`,
`licenses`, `publisher`, `resource_type`, `version`, `funding`, `projects`, `publications`,
`publication_items`, `collected`, `places`, `sources`, `processing_software`, `requirements`,
`abstract`, `series_information`, `table_of_contents`, `technical_info`, `methods` - and its macros:
`person(creator)` (name, ORCID, affiliations), `link(text, url)`, `description(type)` and
`related_identifier(relation, label)`. See the top of `base.md.jinja` for their definitions.

Override `README.md.jinja`, not `base.md.jinja`: an override can not extend the template it replaces.
To give one model its own README, pass another template name:
`AddReadmeExport(template="my_repository/README.md.jinja")`. Runs of blank lines in the output are
collapsed, so templates need no whitespace control.
"""

from __future__ import annotations

# the API for using the export; extension points are imported from the modules. Explicit re-exports
# (PEP 484 `X as X`) not listed in __all__, so the API documentation shows them only on their module page
from .model import AddReadmeExport as AddReadmeExport
