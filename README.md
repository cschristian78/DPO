# Portfolio PML Optimizer

A small self-contained web app that runs **leave-one-out marginal PML analysis**
against your RDM databases on SQL Server.

For one analysis, one granularity (**account / policy / location**) and one loss
perspective (**ground up / gross / net before tax**), it:

1. Takes each account's / policy's / location's ELT out of the portfolio ELT,
   recalculates the EP points, and writes the new PMLs to a table (**EXCL** -
   first row is the portfolio-level PML, then one row per removed entity).
2. Subtracts each excluding-entity PML from the original portfolio PML and
   writes the differences as a second output (**DIFF** - the marginal impact
   of each entity on tail loss).

PML points on every curve: **5, 10, 25, 50, 75, 100, 130, 250, 500, 750, 1000**.

## Deploy (test environment)

Requires Python 3.10+ and the **ODBC Driver for SQL Server** on the machine
running the app (download from Microsoft; the app server just needs the
driver, not SQL Server itself).

```bash
# 1. Unzip the package, e.g. C:\pml-optimizer  (Windows) or /opt/pml-optimizer (Linux)
# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure - copy the example and fill in YOUR values
copy config.example.yaml config.yaml        # Windows
# cp config.example.yaml config.yaml        # Linux

# 4. Edit config.yaml:
#      connection.server / database  -> your SQL Server + RDM database
#      schema.*                      -> your table and column names
#                                      (see "Schema mapping" below)

# 5. Run
python app.py
#    then open http://localhost:5000  (or http://<server>:5000 from a browser)
```

Use a least-privilege SQL login (or domain account) that has SELECT on the
RDM tables and CREATE/INSERT rights in the results schema.

## Use

1. **Connect** - enter server, database, and authentication. The app attaches
   to the SQL Server and verifies the connection. Passwords are typed in the
   UI and held in server memory only; they are never written to disk.
2. **Analyses** - every analysis from `RDM_analysis` is listed, with a
   checkmark per granularity where loss data actually exists (the app
   cross-references each analysis against the account / policy / location
   loss tables). Only available granularities can be selected.
3. **Setup** - pick granularity + perspective, then run. Large portfolios
   take a few minutes.
4. **Results** - two tabs:
   - **EXCL**: portfolio PML row, then one row per removed account/policy/location.
   - **DIFF**: portfolio PML minus excluding-entity PML = marginal contribution.
   
   Both tabs offer CSV download, and the result tables are also written back
   to SQL Server as `PML_EXCL_<timestamp>` / `PML_DIFF_<timestamp>`. The EP
   curve tab charts the portfolio curve with an optional per-entity overlay.

## Schema mapping

RDM databases differ in naming, so the physical schema lives entirely in
`config.yaml` - no table/column names are hard-coded except the analyses
table (`RDM_analysis`). The app validates the mapping on startup and on the
Connect page, and the Analyses page reports per-analysis granularity
availability by actually probing your tables - so a wrong table/column name
shows up immediately as "query failed" rather than silently.

What to fill in per granularity: the ELT-style loss table name, the columns
for analysis id / event id / entity (account, policy, or location) number,
and the loss column for each of the three perspectives.

## Methodology notes

- **Portfolio ELT** is computed as the sum of the entity ELTs at the chosen
  granularity (the entity losses are assumed to partition the portfolio).
- **EP curve**: events ranked by loss; the i-th largest of N events gets
  exceedance probability i/(N+1), i.e. return period (N+1)/i.
- **PML at a target point**: linear interpolation of loss vs exceedance
  probability between bracketing ranked events; capped at the largest
  modelled loss for rarer targets.
- **Leave-one-out**: excluding-entity event losses = portfolio event loss
  minus entity event loss, floored at zero; EP curve rebuilt per entity.
- **DIFF** = portfolio PML - excluding-entity PML (>= 0 by construction).

## Security

- Credentials are entered in the web UI, kept in process memory only, and
  cleared when the app restarts. They never touch disk or logs.
- Run behind your network's normal controls; the app itself has no user
  accounts - anyone who can reach the URL can use it.

## Troubleshooting

- `Connection failed` - check server name reachability from the app machine,
  SQL Server Browser / port 1433, and that the ODBC driver name in
  `config.yaml` matches the installed driver.
- `Could not read RDM_analysis` / `query failed` on a granularity - the
  schema mapping in `config.yaml` doesn't match your database; fix the
  table/column names.
- Slow runs - the computation is vectorized (pandas/numpy); very large
  location counts are the main cost driver (one EP rebuild per entity).
