"""Stop search SQL (parameterised). Rent and neighbourhood statistics live in mcp_server/core.py (one copy for the API and MCP)."""


def stops(conn, q):
    return [{"id": i, "name": n, "lat": la, "lon": lo} for i, n, la, lo in conn.execute(
        """SELECT id, name, ST_Y(ST_Transform(geom,4326)), ST_X(ST_Transform(geom,4326))
           FROM core.stop_group WHERE name ILIKE %s OR name %% %s
           ORDER BY similarity(name, %s) DESC, name LIMIT 10""",
        (f"%{q.replace('%', '').replace('_', '')}%", q, q)).fetchall()]
