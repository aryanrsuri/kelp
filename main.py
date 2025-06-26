import os
import sqlite3
from typing import List, Tuple
import click
from blake3 import blake3
from time import time_ns
from pathlib import Path



"""
```
> kelp start
# Generates a new kelp db
> kelp make 
# Generates an empty event
> kelp edit [<name=value>]
> kelp comment
> kelp list
> kelp show
> kelp ui
"""

class Kelp:
    def __init__(self, root: str = "."):
        self.index = Path(root) / "kelp"
        self.conn = sqlite3.connect(self.index)
        return None

    def ensure_kelp(self):
        try: 
            self.conn = sqlite3.connect(self.index)
            self.conn.row_factory = sqlite3.Row
            self.init_tables()
        except Exception as Ex:
            raise Ex

    def _get_evt_by_prefix(self, evt_prefix: str) -> sqlite3.Row:
        cursor = self.conn.execute("SELECT evt_id, evt_uuid FROM event WHERE evt_uuid LIKE ?"
                                   , (f"{evt_prefix}%",))
        events = cursor.fetchall()
        if not events:
            raise click.ClickException(f"Event with id '{evt_prefix}' not found.")
        if len(events) > 1:
            raise click.ClickException(f"Event with id '{evt_prefix}' is ambigous. Found {len(events)} results.")
        return events[0]

    def _get_blob_by_prefix(self, evt_prefix: str) -> Tuple[int, bytes]:
        event = self._get_evt_by_prefix(evt_prefix)
        cursor = self.conn.execute(
            """
            SELECT b.comment FROM blob b
            JOIN eventlog l ON b.r_id = l.r_id
            WHERE l.evt_id = ? ORDER BY l.evt_mtime DESC LIMIT 1
            """, (event['evt_id'],)
        )
        content_row = cursor.fetchone()
        if not content_row:
             raise click.ClickException("Could not find content for this event.")
        return event['evt_id'], content_row['content']


    def init_tables(self):
        with self.conn:
            self.conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS blob (
                        r_id INTEGER PRIMARY KEY,
                        size INTEGER NOT NULL,
                        uuid TEXT NOT NULL UNIQUE,
                        comment BLOB NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS event (
                        evt_id INTEGER PRIMARY KEY,
                        evt_uuid TEXT UNIQUE,
                        evt_ctime INTEGER,
                        evt_mtime INTEGER,
                        title TEXT,
                        type TEXT,
                        status TEXT,
                        priority TEXT,
                        severity TEXT,
                        resolution TEXT,
                        components TEXT,
                        depends_on TEXT,
                        comment TEXT,
                        found_in TEXT
                    );
                    CREATE TABLE IF NOT EXISTS eventlog (
                        evt_id INTEGER NOT NULL,
                        r_id INTEGER NOT NULL,
                        evt_mtime INTEGER,
                        evt_user TEXT,
                        i_comment TEXT,
                        FOREIGN KEY(evt_id) REFERENCES event(evt_id),
                        FOREIGN KEY(r_id) REFERENCES blob(r_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_evt_uuid ON event(evt_uuid);
                    CREATE INDEX IF NOT EXISTS idx_evtlog_evt_id_mtime ON eventlog(evt_id, evt_mtime);
                    """
            )
        return None

    def make_blob(self, comment: bytes) -> int:
        size = len(comment)
        uuid = blake3(comment).hexdigest()
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("""SELECT r_id FROM blob WHERE uuid = ? """, (uuid,))
            if curr := cursor.fetchone():
                return curr["r_id"]
            cursor.execute("""INSERT INTO blob (size, uuid, comment) VALUES (?, ?, ?)""", (size, uuid, comment))
            return cursor.lastrowid or -1

    def make_event(self, title: str, comment: str) -> str:
        mtime: int = time_ns() // 1000
        evt_uuid = blake3(bytes(title, encoding="utf-8")).hexdigest(32)
        with self.conn:
            r_id = self.make_blob(bytes(comment, encoding="utf-8"))
            cursor = self.conn.cursor()
            cursor.execute(
                    """
                    INSERT INTO event (
                        evt_uuid, evt_mtime, evt_ctime, title
                    ) VALUES (?,  ?,  ?,  ?)
                    """, (evt_uuid, mtime, mtime, title)
                    )
            evt_id = cursor.lastrowid
            if evt_id is None:
                raise Exception("Event unable to insert.")

            cursor.execute(
                    """
                    INSERT INTO eventlog ( evt_id, r_id, evt_mtime, evt_user, i_comment ) 
                    VALUES ( ?, ?, ?, ?, ?)
                    """, (evt_id, r_id, mtime, os.getenv("USER", "anonymous"), comment)
                    )

            return evt_uuid


    def update_event(self, evt_prefix: str, comment: str, **metadata) -> str:
        raise NotImplemented


    def show_event(self, evt_prefix: str) -> Tuple[int, str]:
        evt_id, content = self._get_evt_by_prefix(evt_prefix)
        return evt_id, content



    def list_events(self) -> List[Tuple[str,str]]:
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute(
                    """ SELECT SUBSTR(evt_uuid,0,13), title FROM event """
                    )
            events = cursor.fetchall()
        return events




@click.group()
@click.pass_context
def kelp(ctx):
    """ local event tracker """
    ctx.obj = Kelp(root=".")

@kelp.command()
@click.pass_obj
def new(k: Kelp):
    """ Create a new kelp index """
    k.ensure_kelp()
    click.echo("Kelp index created!")

@kelp.command()
@click.argument("title")
@click.pass_obj
def make(k: Kelp, title: str):
    """ Make a new kelp event """ 
    content = click.edit()
    evt_uuid = k.make_event(title, str(content))
    click.echo(f"Event created: {evt_uuid}")

@kelp.command()
@click.pass_obj
def list(k: Kelp):
    """ List all kelp events """
    events = k.list_events()
    click.echo("event hash\t\t title\n\r")
    for event in events:
        prefix, title = event
        click.echo(f"{prefix}\t\t{title}")

@kelp.command()
@click.argument("evt_prefix")
@click.pass_obj
def show(k: Kelp, evt_prefix: str):
    evt_id, content = k.show_event(evt_prefix)
    click.echo(evt_id)
    click.echo(content)



if __name__=="__main__":
    kelp()
