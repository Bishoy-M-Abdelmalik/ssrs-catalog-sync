"""
ssrs_catalog_sync.py

Extracts, parses, and loads SSRS reports and datasets into a SQL Server
tracking database. Uses the SSRS REST API and in-memory XML parsing.
"""
import io
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import astuple, dataclass
from datetime import datetime
from typing import Dict, List, Optional

import pyodbc
import requests
import yaml
from requests_negotiate_sspi import HttpNegotiateAuth

@dataclass
class CatalogItem:
    """
    Strict schema mapping for the temporary catalog staging table..

    Attributes:
        id (str): The unique GUID of the SSRS item.
        name (str): The name of the report or dataset.
        path (str): The parent directory path.
        virtual_path (Optional[str]): Virtual path mapping (defaults to None).
        type_name (str): The type of object ('Report', 'DataSet', etc.).
        size (int): The file size in bytes.
        description (str): SSRS description field.
        hidden (str): String boolean representing visibility.
        creation_date (Optional[datetime]): Standardized creation timestamp.
        modified_date (Optional[datetime]): Standardized modification timestamp.
        created_by (str): User who created the item.
        modified_by (str): User who last modified the item.
        item_meta_data (Optional[str]): Additional metadata.
    """
    id: str
    name: str
    path: str
    virtual_path: Optional[str]
    type_name: str
    size: int
    description: str
    hidden: str
    creation_date: Optional[datetime]
    modified_date: Optional[datetime]
    created_by: str
    modified_by: str
    item_meta_data: Optional[str]

class SSRSRESTClient:
    """Handles SSRS REST API requests securely using Windows Authentication."""

    def __init__(self, rest_url: str, logger: logging.Logger):
        """
        Initializes the REST client with Windows SSPI Authentication.

        Args:
            rest_url (str): The base URL for the SSRS REST API.
            logger (logging.Logger): The localized logger instance.
        """
        self.logger = logger
        self.rest_base = rest_url.rstrip('/')
        self.session = requests.Session()
        self.session.auth = HttpNegotiateAuth()

    @staticmethod
    def _clean_sql_date(date_str: str) -> Optional[datetime]:
        """
        Converts REST API ISO-8601 dates to SQL-friendly datetime objects.

        Args:
            date_str (str): The raw ISO-8601 date string from the API.

        Returns:
            Optional[datetime]: A native datetime object stripped of timezones, 
                                or None if the input is missing/invalid.
        """
        if not date_str:
            return None # Translates to SQL NULL
        try:
            # Replace 'Z' (Zulu time) with standard UTC offset.
            clean_str = date_str.replace('Z', '+00:00')
            # Convert the string into a native Python datetime object
            dt = datetime.fromisoformat(clean_str)
            # Catch ANY date before SQL Server's 1753 limit
            if dt.year < 1753:
                return None

            return dt.replace(tzinfo=None)  # Strip the timezone info
        except ValueError:
            return None

    @staticmethod
    def _get_parent_path(full_path: str) -> str:
        """
        Transforms absolute file paths into parent directory paths.

        Args:
            full_path (str): The full path of the catalog item (e.g., '/Dir/Report').

        Returns:
            str: The parent directory path (e.g., '/Dir/').
        """
        if not full_path or full_path == '/':
            return '/'
        return os.path.dirname(full_path.rstrip('/')) + '/'

    def fetch_target_catalog_items(self) -> List[CatalogItem]:
        """
        Fetches Reports and Datasets via OData pagination.

        Returns:
            List[CatalogItem]: A strictly typed list of all catalog metadata objects.

        Raises:
            requests.exceptions.RequestException: If the REST API endpoint is unreachable 
                                                    or returns an HTTP error code.
        """
        self.logger.info("Scanning SSRS via REST API for catalog items...")

        url = self.rest_base
        items_scanned = 0
        parsed_items = []

        while url:
            response = self.session.get(url, timeout=60)
            response.raise_for_status()
            data = response.json()

            raw_items = data.get('value', [])
            items_scanned += len(raw_items)

            for item in raw_items:
                parsed_items.append(CatalogItem(
                    id=item.get('Id', None),
                    name=item.get('Name', None),
                    path=self._get_parent_path(item.get('Path', None)),
                    virtual_path=None,
                    type_name=item.get('Type', None),
                    size=item.get('Size', 0),
                    description=item.get('Description', None),
                    hidden=str(item.get('Hidden', None)),
                    creation_date=self._clean_sql_date(item.get('CreatedDate')),
                    modified_date=self._clean_sql_date(item.get('ModifiedDate')),
                    created_by=item.get('CreatedBy', None),
                    modified_by=item.get('ModifiedBy', None),
                    item_meta_data=None
                ))

            url = data.get('@odata.nextLink', None)     # Fetch the next OData pagination link

        self.logger.debug(
            "Retrieved %s actionable items out of %s scanned records."
            ,len(parsed_items), items_scanned)
        return parsed_items

    def download_xml_bytes(self, report_id: str) -> bytes:
        """
        Downloads the raw XML definition of a report directly into memory.

        Args:
            report_id (str): The unique GUID of the target SSRS report.

        Returns:
            bytes: The binary payload of the downloaded XML file.

        Raises:
            requests.exceptions.RequestException: If the download fails or times out.
        """
        url = f"{self.rest_base}({report_id})/Content/$value"
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        return response.content

class SSRSXMLParser:
    """Parses SSRS XML streams entirely in-memory."""

    NAMESPACES = {
        'Report': 'http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition',
        'Dataset': 'http://schemas.microsoft.com/sqlserver/reporting/2010/01/shareddatasetdefinition'
    }

    def __init__(self, base_url: str, logger: logging.Logger):
        """
        Initializes the XML Parser.

        Args:
            base_url (str): The base URL for the SSRS Web Portal.
            logger (logging.Logger): The localized logger instance.
        """
        self.base_url = base_url.rstrip('/')
        self.logger = logger

    @staticmethod
    def extract_sql_variables(query_text: str) -> str:
        """
        Safely extracts SQL variables starting with @ using Regex.

        Args:
            query_text (str): The raw SQL query string from the XML.

        Returns:
            str: A comma-separated string of unique SQL variables.
        """
        if not query_text:
            return ""
        matches = re.findall(r'@\w+', query_text)
        unique_vars = list(dict.fromkeys(matches)) # Preserve order, remove duplicates
        return ', \n'.join(unique_vars)

    def extract_metadata(self, xml_bytes: bytes, report_type: str) -> dict:
        """
        Parses the in-memory bytes and returns a comprehensive metadata dictionary.

        Args:
            xml_bytes (bytes): The raw XML binary payload.
            report_type (str): The type of SSRS object ('Report' or 'DataSet').

        Returns:
            dict: A mapped dictionary containing lists of commands, datasets, 
                  parameters, and linked_objects.

        Raises:
            xml.etree.ElementTree.ParseError: If the XML binary is corrupted or malformed.
        """
        tree = ET.parse(io.BytesIO(xml_bytes))
        root = tree.getroot()

        metadata: Dict[str, list] = {
            "commands": [],
            "parameters": [],
            "linked_objects": []
        }

        prefix = 'Report' if report_type == 'Report' else 'Dataset'

        # 1. Extract Datasets and map Commands
        for ds in root.findall(f".//{prefix}:DataSet", self.NAMESPACES):
            ds_name = ds.attrib.get('Name', 'Embedded')

            # Identify Shared Datasets
            shared_ref = ds.find(
                f"{prefix}:SharedDataSet/{prefix}:SharedDataSetReference", self.NAMESPACES
                )
            if shared_ref is not None and shared_ref.text:
                path_str = shared_ref.text.replace('\"', '')
                metadata["linked_objects"].append({
                    "type": "Shared DataSet",
                    "name": path_str.split('/')[-1],
                    "path": path_str.rsplit('/', 1)[0] + '/',
                    "internal": 1,
                    "given_name": ds_name
                })
            else:

                # Find the Embedded CommandText explicitly tied to this Dataset
                cmd = ds.find(f".//{prefix}:Query/{prefix}:CommandText", self.NAMESPACES)
                if cmd is not None and cmd.text:
                    metadata["commands"].append({
                        "dataset_name": ds_name,
                        "text": cmd.text,
                        "params": self.extract_sql_variables(cmd.text)
                    })

        # 2. Extract Parameters & Variables
        param_mappings = {
            "ReportParameter": "Report Parameter",
            "DataSetParameter": "DataSet Parameter",
            "QueryParameter": "Query Parameter",
            "Variable": "Report Variable"
        }
        for xml_tag, param_type in param_mappings.items():
            for param in root.findall(f".//{prefix}:{xml_tag}", self.NAMESPACES):
                if 'Name' in param.attrib:
                    val_node = param.find(f".//{prefix}:Value", self.NAMESPACES)
                    val_text = val_node.text if (val_node is not None and val_node.text) else 'Prompted'

                    metadata["parameters"].append({
                        "name": param.attrib['Name'],
                        "type": param_type,
                        "value": val_text
                    })

        # 3. Extract Subreports & Drillthroughs
        link_mappings = [("Subreport", "Subreport"), ("Drillthrough", "Drillthrough")]
        for xml_tag, link_type in link_mappings:
            for node in root.findall(f".//{prefix}:{xml_tag}", self.NAMESPACES):
                rep_name_node = node.find(f"{prefix}:ReportName", self.NAMESPACES)
                if rep_name_node is not None and rep_name_node.text:
                    path_str = rep_name_node.text.replace('\"', '')
                    if '=' in path_str:
                        path_str = path_str.split('\"')[-1]

                    metadata["linked_objects"].append({
                        "type": link_type,
                        "name": path_str.split('/')[-1],
                        "path": path_str.rsplit('/', 1)[0] + '/',
                        "internal": 1,
                        "given_name": None
                    })

        # 4. Extract Hyperlinks
        for hyper in root.findall(f".//{prefix}:Hyperlink", self.NAMESPACES):
            if hyper.text:
                hyper_current = urllib.parse.unquote(hyper.text)

                if f'{self.base_url}/reports' in hyper_current:
                    mod_hyper = hyper_current.replace(f'{self.base_url}/reports/report', '')
                    metadata["linked_objects"].append({
                        "type": "HyperLink",
                        "name": mod_hyper.split('/')[-1],
                        "path": mod_hyper.rsplit('/', 1)[0] + '/',
                        "internal": 1,
                        "given_name": None
                    })
                elif 'mailto' in hyper_current.lower():
                    metadata["linked_objects"].append({
                        "type": "HyperLink",
                        "name": None,
                        "path": hyper_current.split(':')[1],
                        "internal": 0,
                        "given_name": None
                    })
                else:
                    clean_hyper = hyper_current.lower()
                    if 'fields!' in clean_hyper:
                        raw_fields = [
                            p.partition('.value')[0].strip()
                            for p in clean_hyper.split('fields!')[1:]
                        ]
                        clean_fields = list(dict.fromkeys(raw_fields))
                        clean_hyper = '\n'.join(clean_fields)

                    metadata["linked_objects"].append({
                        "type": "HyperLink",
                        "name": None,
                        "path": f"Using value named = \n{clean_hyper}",
                        "internal": 0,
                        "given_name": None
                    })

        # 5. Pre-calculate RefQty (Duplicate occurrences count)
        counted_links = []
        counts = Counter(tuple(sorted(obj.items())) for obj in metadata["linked_objects"])

        for unique_tuple, qty in counts.items():
            obj_dict = dict(unique_tuple)
            obj_dict["ref_qty"] = qty
            counted_links.append(obj_dict)

        metadata["linked_objects"] = counted_links
        return metadata

class DatabaseManager:
    """Context manager for isolated pyodbc transactions and delta-syncing."""

    def __init__(self, db_config: dict, logger: logging.Logger):
        """
        Initializes the Database Manager configuration.

        Args:
            db_config (dict): The DB connection parameters extracted from config.yaml.
            logger (logging.Logger): The localized logger instance.
        """
        self.logger = logger
        self.conn_str = (
            f"DRIVER={{{db_config['driver']}}};"
            f"SERVER={db_config['server']};"
            f"DATABASE={db_config['database']};"
            f"Trusted_Connection=yes;MARS_Connection=Yes;"
        )
        self.timeout = db_config.get('timeout', 300)
        self.conn = None

    def __enter__(self):
        """
        Establishes the database connection upon entering context.

        Returns:
            DatabaseManager: The active class instance.

        Raises:
            pyodbc.Error: If the connection string is invalid or server is offline.
        """
        self.conn = pyodbc.connect(self.conn_str, timeout=self.timeout)
        self.logger.info("Database connection established.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Closes the database connection safely upon exiting context, 
        forcing a rollback if an unhandled exception occurred.
        """
        if self.conn:
            if exc_type:
                self.conn.rollback()
                self.logger.warning("Rolled back transactions due to exception.")
            self.conn.close()
            self.logger.info("Database connection closed securely.")

    def sync_catalog_hierarchy(self, catalog_items: List[CatalogItem]) -> list:
        """
        Truncates TMP table, bulk inserts items, and executes CDC Historical Tracking logic.

        Args:
            catalog_items (List[CatalogItem]): The complete list of SSRS catalog objects.

        Returns:
            list: A list of dictionaries representing the delta (new/modified items)
                  that require XML parsing.

        Raises:
            pyodbc.Error: If the bulk insert or MERGE statement fails to execute.
        """
        cursor = self.conn.cursor()

        # Truncate Temporary Table
        cursor.execute("TRUNCATE TABLE catalog.TMP_CatalogItems")

        # Bulk Insert into TMP
        insert_query = r"""
            INSERT INTO catalog.TMP_CatalogItems 
            (ID, Name, Path, Virtual_Path, Type_Name, Size, Description, Hidden, 
             Creation_Date, Modified_Date, Created_By, Modified_By, Item_Meta_Data, 
             IsDeleted, IsModified) 
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)
        """
        # Convert Dataclasses strictly to tuples for safe insertion
        rows = [astuple(item) for item in catalog_items]
        cursor.executemany(insert_query, rows)

        # Identify New & Modified Reports
        new_query = r"""
            SELECT T.ID, T.Name, T.Type_Name, T.Modified_Date 
            FROM catalog.TMP_CatalogItems T 
            WHERE NOT EXISTS (SELECT 1 FROM catalog.CatalogItems O WHERE O.ObjectID = T.ID)
              AND T.Type_Name IN ('Report', 'DataSet')
        """

        modified_query = r"""
            SELECT T.ID, T.Name, T.Type_Name, T.Modified_Date 
            FROM catalog.TMP_CatalogItems T 
            WHERE EXISTS (SELECT 1 FROM catalog.CatalogItems O WHERE O.ObjectID = T.ID)
              AND NOT EXISTS (
                  SELECT 1 FROM catalog.CatalogItems O 
                  WHERE O.ObjectID = T.ID 
                    AND ABS(DATEDIFF(second, O.ModifiedDate, T.Modified_Date)) <= 1
              )
              AND T.Type_Name IN ('Report', 'DataSet')
        """

        items_to_process = []
        for row in cursor.execute(new_query).fetchall():
            items_to_process.append(
                {"id": row[0], "name": row[1], "type": row[2], "modified": row[3], "status": "NEW"}
                )
        for row in cursor.execute(modified_query).fetchall():
            items_to_process.append(
                {"id": row[0], "name": row[1], "type": row[2], "modified": row[3], "status": "MODIFIED"}
                )

        # Insert completely new reports
        self.logger.info("Synchronizing completely new catalog items...")
        cursor.execute(r"""
            INSERT INTO catalog.CatalogItems 
            (ObjectID, ObjectName, ObjectPath, TypeName, Size, [Desc], Hidden, 
             CreationDate, ModifiedDate, CreatedBy, ModifiedBy, IsDeleted, IsModified)
            SELECT T.ID, T.Name, T.Path, T.Type_Name, T.Size, T.Description, T.Hidden, 
                   T.Creation_Date, T.Modified_Date, T.Created_By, T.Modified_By, 0, 0
            FROM catalog.TMP_CatalogItems T
            WHERE NOT EXISTS (SELECT 1 FROM catalog.CatalogItems O WHERE O.ObjectID = T.ID)
        """)

        # Insert new historical records for modified reports
        self.logger.info("Synchronizing new versions of modified catalog items...")
        cursor.execute(r"""
            INSERT INTO catalog.CatalogItems (
                ObjectID, ObjectName, ObjectPath, TypeName, Size, [Desc], Hidden, 
                CreationDate, ModifiedDate, CreatedBy, ModifiedBy, IsDeleted, IsModified
            ) 
            SELECT 
                T.ID, T.Name, T.Path, T.Type_Name, T.Size, T.Description, T.Hidden, 
                T.Creation_Date, T.Modified_Date, T.Created_By, T.Modified_By, 0, 0 
            FROM catalog.TMP_CatalogItems T 
            WHERE EXISTS (SELECT 1 FROM catalog.CatalogItems O WHERE O.ObjectID = T.ID) 
              AND NOT EXISTS (
                  SELECT 1 FROM catalog.CatalogItems O 
                  WHERE O.ObjectID = T.ID 
                    AND ABS(DATEDIFF(second, O.ModifiedDate, T.Modified_Date)) <= 1
              )
        """)

        # Flag older versions of modified reports as IsModified = 1
        cursor.execute(r"""
            UPDATE O
            SET O.IsModified = 1
            FROM catalog.CatalogItems O
            INNER JOIN catalog.TMP_CatalogItems T ON O.ObjectID = T.ID
            WHERE O.ModifiedDate != T.Modified_Date
        """)

        # Flag deleted reports (in DB but no longer on the server)
        cursor.execute(r"""
            UPDATE catalog.CatalogItems
            SET IsDeleted = 1
            WHERE ObjectID NOT IN (SELECT ID FROM catalog.TMP_CatalogItems)
        """)

        # Commit all table synchronizations
        self.conn.commit()
        return items_to_process

    def save_xml_payload(self, report_id: str, report_name: str, mod_date: datetime, xml_content: str):
        """
        Inserts the raw XML string directly into the database.

        Args:
            report_id (str): Target Report ID.
            report_name (str): Target Report Name.
            mod_date (datetime): Target Modification Date.
            xml_content (str): The raw, UTF-16 structured XML string.

        Raises:
            pyodbc.Error: If the SQL insert command fails.
        """
        query = """
            IF NOT EXISTS (SELECT 1 FROM catalog.CatalogXML WHERE ObjectID = ? AND ModifiedDate = ?)
            BEGIN
                INSERT INTO catalog.CatalogXML (ObjectID, ObjectName, ModifiedDate, XMLContent) 
                VALUES (?, ?, ?, ?)
            END
        """
        self.conn.cursor().execute(
            query,
            (report_id, mod_date, report_id, report_name, mod_date, xml_content)
        )
        self.conn.commit()

    def save_parsed_metadata(self, report_id: str, report_name: str,
                             mod_date: datetime, report_type: str, metadata: dict):
        """
        Deletes old metadata for this report, then inserts the newly parsed arrays.

        Args:
            report_id (str): The unique GUID of the SSRS item.
            report_name (str): Target Report Name.
            mod_date (datetime): Target Modification Date.
            metadata (dict): The structured dictionary returned by SSRSXMLParser.
            report_type (str): Target Report Type.

        Raises:
            pyodbc.Error: If the SQL cleanup or insert commands fail.
        """
        cursor = self.conn.cursor()

        # INSERT: Datasets & SQL Queries
        if metadata["commands"]:
            # If multiple queries exist in one report, we iterate through them
            for cmd in metadata["commands"]:

                cursor.execute("""
                    INSERT INTO catalog.CatalogDatasets 
                    (ObjectID, ObjectName, DatasetName, ModifiedDate, ObjectType, QueryDefinition, Params)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (report_id, report_name, cmd["dataset_name"], mod_date,
                        report_type, cmd["text"], cmd["params"]))

        # INSERT: Parameters & Variables
        if metadata["parameters"]:
            unique_params = {(p["name"], p["type"], p["value"]): p
                                for p in metadata["parameters"]}.values()
            param_rows = [
                (report_id, report_name, p["name"], p["type"], p["value"], mod_date)
                for p in unique_params
            ]
            cursor.executemany("""
                INSERT INTO catalog.CatalogParameters 
                (ObjectID, ObjectName, ParamName, ParamType, ParamDefinition, ModifiedDate)
                VALUES (?, ?, ?, ?, ?, ?)
            """, param_rows)

        # INSERT: Linked Objects
        if metadata["linked_objects"]:
            link_rows = []
            for obj in metadata["linked_objects"]:
                link_rows.append((
                    report_id,
                    report_name,
                    obj["type"],
                    obj["path"],
                    obj["name"],
                    obj["name"],
                    obj["path"],
                    obj["ref_qty"],
                    mod_date,
                    obj["internal"],
                    obj["given_name"]
                ))

            cursor.executemany("""
                INSERT INTO catalog.CatalogLinks 
                (ObjectID, ObjectName, LinkType, LinkedID, LinkedName, LinkedPath, 
                 RefQty, ModifiedDate, InternalLink, GivenLinkedName)
                VALUES (?, ?, ?, 
                    (SELECT TOP 1 ObjectID FROM catalog.CatalogItems 
                     WHERE ObjectPath = ? AND ObjectName = ?), 
                    ?, ?, ?, ?, ?, ?)
            """, link_rows)

        self.conn.commit()


def ssrs_catalog_sync(args: list):
    """
    Main entry point triggered by the Python Orchestrator.

    Args:
        args (list): A single array injected by the orchestrator.
            Index 0: session_id (str) - Extracted for telemetry tracing.
            Index 1: working_dir (str) - Designated workspace for logging.
            Index 2+: Dynamic expression arguments.

    Raises:
        Exception: Triggers Exit Code 5. Raised for catastrophic failures 
                (e.g., config missing, DB offline, REST API unreachable).
        Warning: Triggers Exit Code -1. Raised if the job completes but localized 
                parsing errors were intercepted within the payload loop.
        """
    try:
        working_dir = args[1]
    except IndexError as e:
        raise RuntimeError(
            "Required session_id or working_dir arguments were not provided."
        ) from e

    # Establish Standardized Localized Logging
    log_dir = os.path.join(working_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("ssrs_sync")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(
            os.path.join(log_dir, "ssrs_catalog_sync.log"), mode='a', encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)

    logger.info("Commencing SSRS Catalog Sync Job...")

    # Execution Metrics Tracker
    metrics = {
        "total_catalog_items_found": 0,
        "reports_requiring_processing": 0,
        "db_rows_inserted": 0,
        "parse_warnings": 0
    }

    try:
        # Load YAML Configuration
        config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file missing at: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)['ssrs_catalog_sync']

        network = SSRSRESTClient(config['api_endpoint']['rest_url'], logger)
        parser = SSRSXMLParser(config['ssrs']['base_url'], logger)

        with DatabaseManager(config['db_connect'], logger) as db:

            # Step A: Fetch targeted catalog items via OData
            all_catalog_items = network.fetch_target_catalog_items()
            metrics["total_catalog_items_found"] = len(all_catalog_items)

            # Step B: Sync DB to identify what needs XML processing
            pending_reports = db.sync_catalog_hierarchy(all_catalog_items)
            metrics["reports_requiring_processing"] = len(pending_reports)
            logger.info("Identified %s new/modified reports for XML parsing.", len(pending_reports))

            # Step C: Process the delta in memory
            for report in pending_reports:
                report_id = report['id']
                report_name = report['name']

                try:
                    logger.debug("Processing [%s]: %s", report['status'], report_name)

                    # 1. REST API -> Memory Stream
                    xml_bytes = network.download_xml_bytes(report_id)

                    # 2. Memory Stream -> DB (Save Raw XML)
                    xml_string = xml_bytes.decode('iso-8859-1', errors='ignore')

                    first_tag_index = xml_string.find('<')
                    if first_tag_index != -1:
                        xml_string = xml_string[first_tag_index:]

                    # Convert encoding declaration to utf-16 for SQL XML standard
                    xml_string = re.sub(
                        r'<\?xml[^>]*\?>', 
                        '<?xml version=\"1.0\" encoding=\"utf-16\"?>', 
                        xml_string,
                        count=1
                    )
                    db.save_xml_payload(report_id, report_name, report['modified'], xml_string)

                    # 3. Memory Stream -> Metadata Parser
                    parsed_metadata = parser.extract_metadata(xml_bytes, report['type'])

                    # 4. Parsed Data -> DB (Save Datasets, Params, Links)
                    db.save_parsed_metadata(report_id, report_name, report['modified'],
                                            report['type'], parsed_metadata)

                    metrics["db_rows_inserted"] += 1

                except Exception as e:
                    # GRACEFUL DEGRADATION: Catch error, log it, increment warning, and continue
                    logger.warning("Failed to process report '%s' (%s): %s",
                                    report_name, report_id, e)
                    metrics["parse_warnings"] += 1
                    continue

    except Exception as e:
        # Fatal errors bubble up to trigger Exit Code 5
        logger.error("Fatal Pipeline Crash: %s", e)
        raise Exception(f"ssrs_catalog_sync failed: {e}") from e

    finally:
        # Dump execution metrics into the log
        logger.info("Execution Summary: %s", json.dumps(metrics, indent=2))

        # Strict Warning Propagation (Exit Code -1)
        if metrics.get("parse_warnings", 0) > 0:
            logger.warning("Job completed but encountered localized report failures.")
            raise Warning(f"Completed with {metrics['parse_warnings']} parsing errors.")
