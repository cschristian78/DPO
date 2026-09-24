"""Map an RDM database name to an active CMS client.

The client key is the text before the first underscore:
LEFT(database name, CHARINDEX('_', name) - 1).
That value is matched to BMS_CMS.dbo.tClient.ClientShortName.
"""


def client_prefix(database_name):
    name = (database_name or "").strip()
    pos = name.find("_")
    if pos <= 0:
        return ""
    return name[:pos]


def match_client(conn, database_name):
    """Return {clientId, clientName, clientShortName} or None."""
    prefix = client_prefix(database_name)
    if not prefix:
        return None
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TOP (1) ClientID, ClientName, ClientShortName
        FROM dbo.tClient
        WHERE ClientStatus = 'Active'
          AND ClientShortName = ?
        """,
        prefix,
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "clientId": int(row[0]),
        "clientName": row[1] or "",
        "clientShortName": row[2] or "",
    }
