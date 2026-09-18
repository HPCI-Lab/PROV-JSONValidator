# PROV-JSON Validator

A CLI script to validate W3C PROV-JSON files against structural rules, type requirements, prefix declarations, and cross-references.

## Requirements

Python 3.7+ (uses standard library only, no external dependencies).

## Usage

```
python provjson_validator.py <file.json>
```

Validate multiple files at once:

```
python provjson_validator.py file1.json file2.json
```

Options: 
```
--strict: Treat unrecognized "prov:*" attributes as errors instead of warnings.
--no-warnings: Suppress warning messages and only display errors.
--json: Output validation results in JSON format.
```

## What It Checks


- Structure & Syntax: Top-level keys, bundle formatting, and valid JSON structure.
- Prefixes: Verifies that QName prefixes used in entities or attributes are declared in the "prefix" map.
- Identifiers: Ensures entity and activity references exist within the file/bundle.
- Types & Formats: Validates ISO-8601 timestamps and expected datatype structures for reserved PROV attributes.
- Relations: Checks required and optional roles across PROV relation types (e.g., "used", "wasGeneratedBy", "wasDerivedFrom").

## Exit Codes

- 0: All files are valid.
- 1: One or more validation errors were found.
- 2: Unable to read or parse the input file.