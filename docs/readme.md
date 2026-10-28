# README export

This page is part of [oarepo-formats](index.md), which adds import and export formats to the records
of [oarepo-model](https://github.com/oarepo/oarepo-model) based Invenio repositories.

The **README export** (`oarepo_formats.readme`) lets users download a record as a README file in
Markdown. Once the `AddReadmeExport` customization is added to a model, the README is offered on the
record's landing page next to the other export formats and served by the REST API for
`text/markdown`. It is generated from the record's metadata and follows the
[README template](https://github.com/Czech-Data-Steward-Community/README_FILES_EXAMPLES/blob/main/templates/README-MD.md)
of the Czech Data Steward Community.

- **The file** - `<id>.generated.md`, a Markdown README that starts with a notice that it was generated
  automatically.
- **The content** - the record's metadata and the list of its files, arranged as the template above
  describes.
- **Overridable per section and per value** - the repository's template extends the default one and
  replaces only the blocks it wants to change.
- **Also inside the RO-Crate ZIP** - as `README.generated.md`, one of the archive's included exports, if
  the model has both exports.

The full reference (which section comes from which metadata, overriding the template) is in the API
documentation of `oarepo_formats.readme` and its modules.

## Requirements

The README is rendered from the model's DataCite JSON export
(`application/vnd.datacite.datacite+json`), the same requirement as for the RO-Crate export.

## Usage

Add the export to an [oarepo-model](https://github.com/oarepo/oarepo-model) model:

```python
from oarepo_formats.readme import AddReadmeExport

customizations = [
    AddReadmeExport(),
]
```

```bash
curl -H 'Accept: text/markdown' https://<repository>/api/<model>/<id>
```

## Changing the README

Create `<instance>/templates/oarepo_formats/readme/README.md.jinja` (or the same path under an
`APP_THEME` prefix), extend the default template and replace the blocks to change. Every section has
a block, and so does every piece of information in it (`authors`, `contacts`, `dataset_license`, ...),
so that e.g. only the list of authors can be changed:

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

DataCite has no place for variable descriptions, data encoding, missing-value codes, data quality or
ethics; the default README leaves these sections out.

## Tests

The integration tests (`tests/api_tests/test_readme.py`) need the docker services, so run them with
`./run.sh test`.
