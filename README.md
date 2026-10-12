# ssrs-catalog-sync

A Python utility that extracts, parses, and synchronizes SQL Server Reporting Services (SSRS) catalog items into a tracking database.

It queries the SSRS REST API, downloads report and dataset XML definitions entirely in memory, extracts SQL queries, parameters, and linked objects, then performs a delta sync against a SQL Server schema.

## Features

- SSRS REST API (OData) with Windows Authentication
- Full in-memory XML parsing (no temporary files written to disk)
- Delta detection (new / modified / deleted items)
- Extraction of:
  - Embedded SQL queries and variables
  - Report, Dataset, and Query parameters
  - Subreports, Drillthroughs, and Hyperlinks
- Historical versioning of catalog items
- Graceful degradation on individual parse failures

## Prerequisites

- Python 3.9+
- Microsoft ODBC Driver 17 (or later) for SQL Server
- Python packages:

```bash
pip install requests requests_negotiate_sspi pyodbc PyYAML
```

## Configuration

Edit `config.yaml`:

```yaml
ssrs_catalog_sync:
  db_connect:
    server: "your-sql-server"
    database: "catalog_db"
    driver: "ODBC Driver 17 for SQL Server"
    timeout: 300

  api_endpoint:
    rest_url: "https://your-ssrs-server/reports/api/v2.0/CatalogItems"

  ssrs:
    base_url: "https://your-ssrs-server"
```

## Usage

```python
from ssrs_catalog_sync import ssrs_catalog_sync

# Arguments: [session_id, working_dir]
ssrs_catalog_sync(["SESSION_001", "/path/to/workspace"])
```

Or from the command line:

```bash
python -c "from ssrs_catalog_sync import ssrs_catalog_sync; ssrs_catalog_sync(['SESSION_001', '/tmp/workspace'])"
```

## Project Layout

```
ssrs-catalog-sync/
├── ssrs_catalog_sync.py   # Main entry point, REST client, XML parser, and DB manager
├── config.yaml            # Configuration for SSRS API and database
└── README.md
```

## Expected Database Schema

| Table                        | Purpose                                      |
|------------------------------|----------------------------------------------|
| `catalog.TMP_CatalogItems`   | Staging table used for delta detection       |
| `catalog.CatalogItems`       | Historical catalog metadata                  |
| `catalog.CatalogXML`         | Raw XML payloads                             |
| `catalog.CatalogDatasets`    | Extracted SQL queries and dataset names      |
| `catalog.CatalogParameters`  | Report / dataset / query parameters          |
| `catalog.CatalogLinks`       | Subreports, drillthroughs, and hyperlinks    |

## How It Works

1. **Fetch** – Calls the SSRS REST API and retrieves the full catalog (with OData pagination).
2. **Stage** – Loads the catalog into a temporary table.
3. **Diff** – Identifies new and modified Reports/DataSets by comparing modification timestamps.
4. **Download & Parse** – For each changed item, downloads the XML definition into memory and extracts:
   - SQL CommandText and `@variables`
   - Parameters and report variables
   - Subreports, Drillthroughs, and Hyperlinks
5. **Persist** – Writes the raw XML and the structured metadata into the tracking tables.
6. **Mark** – Flags older versions as modified and items that no longer exist as deleted.

## Authentication

The tool uses Windows Integrated Authentication (`requests_negotiate_sspi`). No passwords are stored in configuration files or source code.

## Error Handling

- Individual report parse failures are logged as warnings and skipped.
- A fatal error (missing config, unreachable API, database offline, etc.) raises an exception.
- If any parse warnings occurred, a `Warning` is raised at the end so the caller can treat the run as partially successful.

## License

```text
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.```
