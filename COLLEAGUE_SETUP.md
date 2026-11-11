# Kmart CFC Last Mile Cost Estimator

This package contains the application and source code for the CFC last-mile
cost estimator. It does not contain credentials, Snowflake data, local invoice
files, generated outputs, or the existing virtual environment.

The included source is the production runtime needed for the Streamlit app,
daily and weekly refreshes, invoice workflows, and Consignment Explorer. The
exploratory profiling and prototype utilities are intentionally not included.

## 1. Install

Install the following on the colleague's Windows machine:

- Python 3.13 (64-bit recommended)
- PowerShell 5 or later
- Access to the required Snowflake account, databases, schemas, tables, and
  warehouses
- Optional: VS Code and the Snowflake extension for development

Git is not required to run this ZIP package. No separate Snowflake ODBC driver
is required for the Python application because Snowpark is installed from
`requirements.txt`.

## 2. Create the environment

Open PowerShell in the extracted package directory and run:

```powershell
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell activation is optional. Using the direct path to
`.venv\Scripts\python.exe` avoids execution-policy problems and makes it
clear which Python installation is running the job.

To check the installation without starting a data refresh:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

## 3. Configure Snowflake and local settings

### Snowflake connection

Create or obtain a named Snowflake connection in the colleague's user profile
at:

```text
%USERPROFILE%\.snowflake\connections.toml
```

The default profile name expected by this package is `tsfpa_app_dev`. The
profile must contain the organisation-approved account, user, authenticator,
warehouse, database, schema, and role settings. For example, the shape is:

```toml
[tsfpa_app_dev]
account = "<approved-account>"
user = "<colleague-user>"
authenticator = "externalbrowser"
warehouse = "<approved-warehouse>"
database = "KSFPA"
schema = "ONLINE_PVT"
role = "<approved-role>"
```

Use the organisation's approved authentication method. Do not put passwords,
private keys, tokens, or MFA secrets in this package or commit them to source
control.

If the connection has a different name, set this PowerShell environment
variable before running the app or a pipeline:

```powershell
$env:LM_SNOWFLAKE_CONNECTION = "my_connection_name"
```

The source uses `tsfpa_app_dev` when this variable is not set.

### Optional date and path settings

Most users should let the daily refresh choose its own 14-day window. For a
deliberate extraction window, set:

```powershell
$env:LM_EXTRACTION_START_DATE = "2026-08-20"
$env:LM_EXTRACTION_END_DATE = "2026-09-02"
```

The pipeline also supports these optional variables when running a targeted
or isolated job:

- `LM_OUTPUT_DIR`: output directory for generated Stage 1-3 files
- `LM_INVOICE_FILTER_PATH`: CSV containing an `invoice_number` column
- `LM_STAGE1_INPUT_PATH`, `LM_STAGE1_PARCEL_CONTENTS_PATH`: targeted Stage 1 inputs
- `LM_STAGE2_INPUT_PATH`, `LM_STAGE2_OUTPUT_PATH`: targeted Stage 2 paths
- `LM_STAGE3_OUTPUT_PATH`: targeted Stage 3 output path

### Local input files

The ZIP intentionally excludes potentially sensitive or large operational
inputs. Copy approved files into these paths only when using those workflows:

- Carrier invoice workbooks: `data\inputs\carrier_invoices\`
- Box-suite files: `data\inputs\box_suite\`

The Stage 1-3 Snowflake pipeline can run without carrier invoice workbooks.
Invoice reconciliation requires the relevant invoice files or the supported
Australia Post Snowflake source. Box-suite files are optional; if they are
absent, the relevant box-suite enrichment will not produce matches.

## 4. Run the code

### Verify Snowflake access

This performs a small connection query and does not run the estimator:

```powershell
& .\.venv\Scripts\python.exe -m src.smoke_test_snowflake
```

### Refresh the operational data

The recommended daily command runs Stage 1, Stage 2, Stage 3, and persists the
rolling live reconciliation history:

```powershell
& .\.venv\Scripts\python.exe -m src.daily_refresh
```

For a specified lookback and end date:

```powershell
& .\.venv\Scripts\python.exe -m src.daily_refresh --lookback-days 14 --end-date 2026-09-02
```

Run one refresh at a time. The job writes generated files under `outputs\`, a
status file at `outputs\refresh_status.json`, and logs under
`outputs\refresh_logs\`.

### Rebuild weekly history

This is a separate, slower operation and is normally scheduled weekly rather
than run after every daily refresh:

```powershell
& .\.venv\Scripts\python.exe -m src.history_refresh
```

### Start the Streamlit app

Run this in a second PowerShell window so the app remains available while a
refresh runs:

```powershell
& .\.venv\Scripts\python.exe -m streamlit run .\app\streamlit_app.py
```

Open the local URL printed by Streamlit, normally
`http://localhost:8501`. After a refresh completes, use the app's refresh
control or reload the page to read the new files. Starting Streamlit does not
run the Snowflake pipeline.

In **Invoice reconciliation**, use **Check for new invoice data** first. The
results show AP Snowflake availability and whether the Excel workbooks for
Couriers Please, GoPeople, and Allied are new or changed since their last
processed control summary. Select a carrier and use its refresh button to run
only that carrier's ingest, reconciliation, and live coverage update.

### Invoice reconciliation

Only run this when the required invoice source is available. It is slower than
the daily operational refresh:

```powershell
& .\.venv\Scripts\python.exe -m src.daily_refresh --with-invoice-reconciliation
```

## 5. Common issues

### PowerShell says script execution is disabled

Do not activate the environment. Use the direct executable form shown above:

```powershell
& .\.venv\Scripts\python.exe -m src.daily_refresh
```

This does not require changing the machine execution policy.

### The app says no Stage 3 rows are available

The ZIP excludes generated `outputs`. Run the daily refresh successfully first,
then reload the app. Check the log path printed by the refresh if it fails.

### Snowflake login or MFA fails

Check that the named profile exists at `%USERPROFILE%\.snowflake\connections.toml`,
that `LM_SNOWFLAKE_CONNECTION` matches its name, and that the user has access
to the project tables. External-browser authentication may open a browser
window and wait for MFA completion.

### A refresh says another refresh is already running

Only one daily or history refresh may run at a time. Check the process and
`outputs\refresh_status.json` before taking action. Do not delete
`outputs\refresh_live.lock` while a refresh is still running.

### OneDrive reports Access Denied while writing status

Close any application holding the status file and allow OneDrive to finish
syncing. The refresh code attempts a fallback status file, but a local folder
outside OneDrive is usually more reliable for long-running jobs.

### Invoice or box-suite files are missing

Those files are intentionally not included. Copy the approved source files into
the matching `data\inputs\` subfolder and rerun the relevant ingest or refresh
command.

### The app is slow or shows stale values

The Streamlit process and the refresh process are separate. Keep Streamlit in
one terminal and run refreshes in another. Wait for the refresh to finish, then
reload the app; the app's cached-data control does not start a pipeline.

## What was intentionally excluded

The ZIP excludes `.git`, `.github`, `.venv`, `data\inputs`, `outputs`, Python
bytecode, pytest and Streamlit caches, logs, temporary files, local IDE files,
and other generated artifacts. These exclusions keep the package small and
prevent operational data or credentials being shared accidentally.
