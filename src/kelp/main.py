import os
import sqlite3
import time
import difflib
from typing import List, Tuple, Dict, Optional
import click
from blake3 import blake3
from pathlib import Path

KELP_DIR = ".kelp"
DB_NAME = "kelp.db"
INDEX_FILE = "INDEX"
ROOT_CHECKIN_UUID = "0" * 64

SCHEMA = """
-- Blobs store any raw data (file content, event comments, etc.)
CREATE TABLE IF NOT EXISTS blob (
    blob_hash TEXT PRIMARY KEY, 
    content BLOB NOT NULL
);

-- Checkins represent a snapshot of the repository at a point in time
CREATE TABLE IF NOT EXISTS checkin (
    checkin_uuid TEXT PRIMARY KEY,    
    parent_uuid TEXT,                
    user TEXT,
    timestamp INTEGER NOT NULL,
    comment TEXT
);

-- The manifest links a checkin to the files it contains
CREATE TABLE IF NOT EXISTS manifest (
    checkin_uuid TEXT NOT NULL,
    filepath TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    PRIMARY KEY (checkin_uuid, filepath),
    FOREIGN KEY(checkin_uuid) REFERENCES checkin(checkin_uuid),
    FOREIGN KEY(blob_hash) REFERENCES blob(blob_hash)
);

-- Event tracking tables (from your original code, slightly adapted)
CREATE TABLE IF NOT EXISTS event (
    evt_id INTEGER PRIMARY KEY,
    evt_uuid TEXT UNIQUE NOT NULL,
    evt_ctime INTEGER NOT NULL,
    evt_mtime INTEGER NOT NULL,
    title TEXT,
    status TEXT DEFAULT 'open',
    comment_hash TEXT NOT NULL, 
    FOREIGN KEY(comment_hash) REFERENCES blob(blob_hash)
);

CREATE TABLE IF NOT EXISTS eventlog (
    log_id INTEGER PRIMARY KEY,
    evt_uuid TEXT NOT NULL,
    log_mtime INTEGER NOT NULL,
    log_user TEXT,
    log_comment TEXT, 
    details_hash TEXT,
    FOREIGN KEY(evt_uuid) REFERENCES event(evt_uuid),
    FOREIGN KEY(details_hash) REFERENCES blob(blob_hash)
);

CREATE TABLE IF NOT EXISTS event_checkin_link (
    evt_uuid TEXT NOT NULL,
    checkin_uuid TEXT NOT NULL,
    link_type TEXT,
    link_mtime INTEGER,
    PRIMARY KEY (evt_uuid, checkin_uuid),
    FOREIGN KEY(evt_uuid) REFERENCES event(evt_uuid) ON DELETE CASCADE,
    FOREIGN KEY(checkin_uuid) REFERENCES checkin(checkin_uuid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_link_evt_uuid ON event_checkin_link(evt_uuid);
CREATE INDEX IF NOT EXISTS idx_link_checkin_uuid ON event_checkin_link(checkin_uuid);
CREATE INDEX IF NOT EXISTS idx_checkin_timestamp ON checkin(timestamp);
CREATE INDEX IF NOT EXISTS idx_evt_uuid ON event(evt_uuid);
"""

class Kelp:
    """Manages a Kelp repository."""

    def __init__(self, root: str = "."):
        self.root_path = Path(root).resolve()
        self.kelp_path = self.root_path / KELP_DIR
        self.db_path = self.kelp_path / DB_NAME
        self.index_path = self.kelp_path / INDEX_FILE
        self.conn = None

    def _connect_db(self):
        """Establish a database connection if not already present."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row

    def init_repo(self):
        """Initializes a new Kelp repository."""
        if self.kelp_path.exists():
            raise click.ClickException(f"Kelp repository already exists in {self.kelp_path}")
        
        self.kelp_path.mkdir()
        self.index_path.touch()
        self.manifest_path.touch()
        
        self._connect_db()
        with self.conn:
            self.conn.executescript(SCHEMA)
        
        click.echo(f"Initialized empty Kelp repository in {self.kelp_path}")

    def _ensure_repo(self):
        """Ensures that the command is run from within a Kelp repository."""
        if not self.kelp_path.is_dir() or not self.db_path.is_file():
            raise click.ClickException("Not a Kelp repository. Use 'kelp init' to create one.")
        self._connect_db()


    def _add_blob(self, content: bytes) -> str:
        """Adds content to the blob store if it doesn't exist. Returns the hash."""
        hash_hex = blake3(content).hexdigest()
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1 FROM blob WHERE blob_hash = ?", (hash_hex,))
            if cursor.fetchone() is None:
                cursor.execute("INSERT INTO blob (blob_hash, content) VALUES (?, ?)", (hash_hex, content))
        return hash_hex

    def _get_current_checkin_uuid(self) -> Optional[str]:
        """Reads the current checkin UUID from the INDEX file."""
        content = self.index_path.read_text().strip()
        return content if content else None

    def _walk_project_files(self) -> Dict[str, str]:
        """
        Walks the project directory, ignoring dotfiles/dotdirs, and returns a 
        manifest dict {filepath: content_hash}.
        """
        manifest = {}
        
        for root, dirs, files in os.walk(self.root_path, topdown=True):
            if KELP_DIR in dirs:
                dirs.remove(KELP_DIR)
            
            for dir_name in dirs[:]: 
                if dir_name.startswith('.'):
                    dirs.remove(dir_name)

            for filename in files:
                if filename.startswith('.'):
                    continue

                full_path = Path(root) / filename
                relative_path = str(full_path.relative_to(self.root_path))
                
                try:
                    content = full_path.read_bytes()
                    content_hash = self._add_blob(content)
                    manifest[relative_path] = content_hash
                except IOError as e:
                    click.secho(f"Warning: Could not read file {relative_path}. Skipping. ({e})", fg='yellow')

        return manifest

    def _get_full_uuid(self, prefix: str, table: str, column: str) -> str:
        """Finds a full UUID from a prefix in a given table."""
        if len(prefix) == 64: # Already a full UUID
            return prefix
        
        cursor = self.conn.execute(f"SELECT {column} FROM {table} WHERE {column} LIKE ?", (f"{prefix}%",))
        results = cursor.fetchall()
        if not results:
            raise click.ClickException(f"No revision found with prefix '{prefix}'")
        if len(results) > 1:
            raise click.ClickException(f"Revision prefix '{prefix}' is ambiguous. Found {len(results)} matches.")
        return results[0][0]

    def _get_parent_uuid(self, checkin_uuid: str) -> Optional[str]:
        """Retrieves the parent UUID for a given checkin."""
        cursor = self.conn.execute("SELECT parent_uuid FROM checkin WHERE checkin_uuid = ?", (checkin_uuid,))
        row = cursor.fetchone()
        if row and row['parent_uuid'] != ROOT_CHECKIN_UUID:
            return row['parent_uuid']
        return None

    def _get_manifest(self, checkin_uuid: str) -> Dict[str, str]:
        """Retrieves the manifest for a given checkin UUID."""
        if checkin_uuid == ROOT_CHECKIN_UUID:
            return {}
        cursor = self.conn.execute(
            "SELECT filepath, blob_hash FROM manifest WHERE checkin_uuid = ?",
            (checkin_uuid,)
        )
        return {row['filepath']: row['blob_hash'] for row in cursor.fetchall()}

    def _get_blob_content(self, blob_hash: str) -> bytes:
        """Retrieves content from the blob store."""
        cursor = self.conn.execute("SELECT content FROM blob WHERE blob_hash = ?", (blob_hash,))
        row = cursor.fetchone()
        return row['content'] if row else b''

    def update_event(self, evt_prefix: str, new_title: Optional[str], new_status: Optional[str], new_comment: Optional[str]):
        """
        Updates an event's properties and creates a log entry for the changes.
        """
        self._ensure_repo()
        
        evt_uuid = self._get_full_uuid(evt_prefix, "event", "evt_uuid")
        
        # Get the current state of the event
        cursor = self.conn.execute("SELECT * FROM event WHERE evt_uuid = ?", (evt_uuid,))
        old_event = cursor.fetchone()
        if not old_event:
            raise click.ClickException(f"Event {evt_prefix} not found.")

        _, old_comment_content = self.show_event(evt_uuid)

        update_clauses = []
        update_values = []
        log_entries = []
        
        if new_title is not None and new_title != old_event['title']:
            update_clauses.append("title = ?")
            update_values.append(new_title)
            log_entries.append(f"title changed to '{new_title}'")

        if new_status is not None and new_status != old_event['status']:
            update_clauses.append("status = ?")
            update_values.append(new_status)
            log_entries.append(f"status changed from '{old_event['status']}' to '{new_status}'")
            
        if new_comment is not None and new_comment != old_comment_content:
            new_comment_hash = self._add_blob(new_comment.encode('utf-8'))
            if new_comment_hash != old_event['comment_hash']:
                update_clauses.append("comment_hash = ?")
                update_values.append(new_comment_hash)
                log_entries.append("description updated")

        # If no actual changes were made, do nothing.
        if not update_clauses:
            click.echo("No changes detected.")
            return False

        # Always update the modification time
        mtime = int(time.time())
        update_clauses.append("evt_mtime = ?")
        update_values.append(mtime)
        
        # --- Execute transaction ---
        with self.conn:
            # 1. Update the main event table
            query = f"UPDATE event SET {', '.join(update_clauses)} WHERE evt_uuid = ?"
            update_values.append(evt_uuid)
            self.conn.execute(query, tuple(update_values))

            # 2. Insert a new record into the event log
            log_comment = "; ".join(log_entries)
            self.conn.execute(
                "INSERT INTO eventlog (evt_uuid, log_mtime, log_user, log_comment) VALUES (?, ?, ?, ?)",
                (evt_uuid, mtime, os.getenv("USER", "anon"), log_comment)
            )
        
        click.echo(f"Event {evt_uuid[:12]} updated.")
        return True

    def checkin(self, comment: str):
        """Creates a new checkin (commit) of the current project state."""
        self._ensure_repo()
        
        timestamp = int(time.time())
        parent_uuid = self._get_current_checkin_uuid() or ROOT_CHECKIN_UUID
        
        click.echo("Scanning files...")
        manifest_data = self._walk_project_files()
        
        if not manifest_data:
            raise click.ClickException("Cannot check in an empty project.")
            
        # Create a stable string from the manifest to hash for the checkin UUID
        manifest_string = "".join(sorted([f"{f}:{h}" for f, h in manifest_data.items()]))
        
        # Hash the combination of parent, manifest, and timestamp to create a unique checkin UUID
        hasher = blake3()
        hasher.update(parent_uuid.encode())
        hasher.update(manifest_string.encode())
        hasher.update(str(timestamp).encode())
        hasher.update(comment.encode())
        checkin_uuid = hasher.hexdigest()

        with self.conn:
            self.conn.execute(
                "INSERT INTO checkin (checkin_uuid, parent_uuid, user, timestamp, comment) VALUES (?, ?, ?, ?, ?)",
                (checkin_uuid, parent_uuid, os.getenv("USER", "anonymous"), timestamp, comment)
            )
            # 2. Insert all manifest entries for this checkin
            manifest_rows = [(checkin_uuid, fp, h) for fp, h in manifest_data.items()]
            self.conn.executemany(
                "INSERT INTO manifest (checkin_uuid, filepath, blob_hash) VALUES (?, ?, ?)",
                manifest_rows
            )

        self.index_path.write_text(checkin_uuid)

        click.echo(f"Checked in as [{checkin_uuid[:12]}]")

    def log(self):
        """Shows the checkin history."""
        self._ensure_repo()
        cursor = self.conn.execute("SELECT * FROM checkin WHERE checkin_uuid != ? ORDER BY timestamp DESC", (ROOT_CHECKIN_UUID,))
        for row in cursor.fetchall():
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['timestamp']))
            click.echo(f"checkin: {row['checkin_uuid']}")
            click.echo(f"Parent:  {row['parent_uuid']}")
            click.echo(f"Date:    {ts}")
            click.echo(f"\n\t{row['comment']}\n")

    def make_event(self, title: str, description: str) -> str:
        self._ensure_repo()
        mtime = int(time.time())
        
        evt_uuid = blake3(f"{title}{mtime}".encode()).hexdigest()
        desc_hash = self._add_blob(description.encode())
        current_checkin = self._get_current_checkin_uuid()
        
        if not current_checkin:
            raise click.ClickException(
                    "Cannot create an event. There are no checkins that exist in the repository."
            )
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO event (evt_uuid, evt_ctime, evt_mtime, title, comment_hash) VALUES (?, ?, ?, ?, ?)",
                (evt_uuid, mtime, mtime, title, desc_hash)
            )
            
            cursor.execute(
                "INSERT INTO eventlog (evt_uuid, log_mtime, log_user, log_comment, details_hash) VALUES (?, ?, ?, ?, ?)",
                (evt_uuid, mtime, os.getenv("USER", "anon"), f"Event created: {title}", desc_hash)
            )

            cursor.execute(
                """INSERT INTO event_checkin_link (evt_uuid, checkin_uuid, link_type, link_mtime)
                VALUES (?,?,'created_at', ?)""", (evt_uuid, current_checkin, mtime)
            )

        return evt_uuid

    def link_event_to_checkin(self, evt_prefix: str, checkin_prefix: str):
        """Manually links an event to a checkin."""
        self._ensure_repo()
        evt_uuid = self._get_full_uuid(evt_prefix, "event", "evt_uuid")
        checkin_uuid = self._get_full_uuid(checkin_prefix, "checkin", "checkin_uuid")

        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO event_checkin_link (evt_uuid, checkin_uuid, link_type, link_mtime) VALUES (?, ?, ?, ?)",
                (evt_uuid, checkin_uuid, 'manual', int(time.time()))
            )
        click.echo(f"Linked event {evt_uuid[:12]} to check-in {checkin_uuid[:12]}")

    def get_linked_events(self, checkin_uuid: str):
        """Gets all events linked to a specific checkin."""
        cursor = self.conn.execute(
            """
            SELECT e.evt_uuid, e.title, l.link_type FROM event_checkin_link l
            JOIN event e ON l.evt_uuid = e.evt_uuid
            WHERE l.checkin_uuid = ?
            """, (checkin_uuid,)
        )
        return cursor.fetchall()

    def get_linked_checkins(self, evt_uuid: str):
        """Gets all checkins linked to a specific event."""
        cursor = self.conn.execute(
            """
            SELECT c.checkin_uuid, c.comment, l.link_type FROM event_checkin_link l
            JOIN checkin c ON l.checkin_uuid = c.checkin_uuid
            WHERE l.evt_uuid = ?
            """, (evt_uuid,)
        )
        return cursor.fetchall()

    def list_events(self) -> List[sqlite3.Row]:
        self._ensure_repo()
        cursor = self.conn.execute("SELECT evt_uuid, title, status, evt_mtime FROM event ORDER BY evt_mtime DESC")
        return cursor.fetchall()
        
    def show_event(self, evt_prefix: str) -> Tuple[sqlite3.Row, str]:
        self._ensure_repo()
        cursor = self.conn.execute("SELECT * FROM event WHERE evt_uuid LIKE ?", (f"{evt_prefix}%",))
        events = cursor.fetchall()
        if not events:
            raise click.ClickException(f"Event with id '{evt_prefix}' not found.")
        if len(events) > 1:
            raise click.ClickException(f"Event id '{evt_prefix}' is ambiguous. Found {len(events)} results.")
        
        event_row = events[0]
        cursor.execute("SELECT content FROM blob WHERE blob_hash = ?", (event_row['comment_hash'],))
        content_row = cursor.fetchone()
        
        if not content_row:
             raise click.ClickException("Could not find content for this event.")
        
        return event_row, content_row['content'].decode()

    def diff(self, old_uuid: str, new_uuid: str):
        """Compares two checkins and prints a colored diff."""
        self._ensure_repo()
        
        manifest_old = self._get_manifest(old_uuid)
        manifest_new = self._get_manifest(new_uuid)

        all_files = sorted(set(manifest_old.keys()) | set(manifest_new.keys()))

        for file in all_files:
            hash_old = manifest_old.get(file)
            hash_new = manifest_new.get(file)

            if hash_old == hash_new:
                continue

            # Use unified_diff for all cases for consistent output format
            content_old_bytes = self._get_blob_content(hash_old) if hash_old else b''
            content_new_bytes = self._get_blob_content(hash_new) if hash_new else b''
            
            # Decode safely, replacing errors for binary files
            lines_old = content_old_bytes.decode('utf-8', errors='replace').splitlines()
            lines_new = content_new_bytes.decode('utf-8', errors='replace').splitlines()

            from_file = f"a/{file}" if hash_old else "/dev/null"
            to_file = f"b/{file}" if hash_new else "/dev/null"

            diff_header = f"diff --kelp a/{file} b/{file}\n"
            if hash_old is None:
                diff_header += f"new file\n--- /dev/null\n+++ b/{file}\n"
            elif hash_new is None:
                diff_header += f"deleted file\n--- a/{file}\n+++ /dev/null\n"
            else:
                diff_header += f"--- a/{file}\n+++ b/{file}\n"
            
            click.secho(diff_header, fg='yellow', bold=True, nl=False)
            
            diff_lines = difflib.unified_diff(lines_old, lines_new, lineterm='', fromfile=from_file, tofile=to_file)

            for line in diff_lines:
                if line.startswith('+'):
                    click.secho(line, fg='green')
                elif line.startswith('-'):
                    click.secho(line, fg='red')
                elif line.startswith('@@'):
                    click.secho(line, fg='cyan')
                else:
                    click.echo(line)

    def get_log(self):
        """
        Returns a list of all checkins, newest first, including a concatenated
        list of linked events (with link metadata) for each checkin.
        """
        self._ensure_repo()
        query = """
        SELECT
            c.checkin_uuid,
            c.parent_uuid,
            c.timestamp,
            c.comment,
            -- NEW FORMAT: 'uuid|title|link_type|link_mtime'
            GROUP_CONCAT(
                e.evt_uuid || '|' || e.title || '|' || l.link_type || '|' || l.link_mtime,
                '||'
            ) AS linked_events_str
        FROM
            checkin c
        LEFT JOIN
            event_checkin_link l ON c.checkin_uuid = l.checkin_uuid
        LEFT JOIN
            event e ON l.evt_uuid = e.evt_uuid
        WHERE
            c.checkin_uuid != :root
        GROUP BY
            c.checkin_uuid
        ORDER BY
            c.timestamp DESC
        """
        cursor = self.conn.execute(query, {'root': ROOT_CHECKIN_UUID})
        return cursor.fetchall()

    def get_checkin_details(self, checkin_uuid):
            """
            Returns metadata, file list, and diffs for a single checkin.
            """
            self._ensure_repo()
            cursor = self.conn.execute("SELECT * FROM checkin WHERE checkin_uuid = ?", (checkin_uuid,))
            checkin_row = cursor.fetchone()
            if not checkin_row:
                return None

            # Get file list and diffs by comparing with the parent
            parent_uuid = checkin_row['parent_uuid']
            manifest_old = self._get_manifest(parent_uuid)
            manifest_new = self._get_manifest(checkin_uuid)
            
            files = []
            all_filepaths = sorted(set(manifest_old.keys()) | set(manifest_new.keys()))

            for path in all_filepaths:
                hash_old = manifest_old.get(path)
                hash_new = manifest_new.get(path)
                if hash_old == hash_new:
                    continue
                
                status = "Modified"
                if hash_old is None:
                    status = "Added"
                elif hash_new is None:
                    status = "Deleted"
                
                content_old = self._get_blob_content(hash_old).decode(errors='replace') if hash_old else ""
                content_new = self._get_blob_content(hash_new).decode(errors='replace') if hash_new else ""

                from .ui import create_side_by_side_diff
                diff_lines = create_side_by_side_diff(content_old, content_new)

                files.append({'path': path, 'status': status, 'diff_lines': diff_lines})

            return {'checkin': checkin_row, 'files': files}

    def get_checkin_context(self, timestamp: int, limit: int = 3):
        """
        Gets a few checkins before and after a given timestamp for context.
        """
        self._ensure_repo()
        query = """
        SELECT * FROM (
            SELECT * FROM checkin 
            WHERE timestamp < :ts AND checkin_uuid != :root
            ORDER BY timestamp DESC LIMIT :lim
        )
        UNION ALL
        SELECT * FROM (
            SELECT * FROM checkin
            WHERE timestamp >= :ts AND checkin_uuid != :root
            ORDER BY timestamp ASC LIMIT :lim
        )
        ORDER BY timestamp DESC
        """
        cursor = self.conn.execute(query, {'ts': timestamp, 'lim': limit, 'root': ROOT_CHECKIN_UUID})
        return cursor.fetchall()

    def get_file_diff(self, checkin_uuid, filepath):
        """Gets the content of a file from a checkin and its parent for diffing."""
        self._ensure_repo()
        details = self.get_checkin_details(checkin_uuid)
        if not details:
            return None
        
        parent_uuid = details['checkin']['parent_uuid']
        manifest_old = self._get_manifest(parent_uuid)
        manifest_new = self._get_manifest(checkin_uuid)

        hash_old = manifest_old.get(filepath)
        hash_new = manifest_new.get(filepath)

        content_old = self._get_blob_content(hash_old).decode(errors='replace') if hash_old else ""
        content_new = self._get_blob_content(hash_new).decode(errors='replace') if hash_new else ""

        return {'old': content_old, 'new': content_new}

    def get_event_log(self, evt_uuid: str):
        """Returns the full changelog for a single event, newest first."""
        self._ensure_repo()
        cursor = self.conn.execute(
            "SELECT * FROM eventlog WHERE evt_uuid = ? ORDER BY log_mtime DESC",
            (evt_uuid,)
        )
        return cursor.fetchall()


@click.group()
@click.pass_context
def kelp(ctx):
    """A simple, single-trunk SCM and event tracker."""
    ctx.obj = Kelp()

# --- SCM Commands ---
@kelp.command()
@click.pass_obj
def init(k: Kelp):
    """Creates a new Kelp repository in the current directory."""
    k.init_repo()

@kelp.command()
@click.option("-m", "--message", help="Checkin message.")
@click.pass_obj
def checkin(k: Kelp, message: str):
    """Records changes to the repository."""
    if not message:
        # Use click.edit() to get a multi-line message from the user's default editor
        #marker = '# Please enter the checkin message for your changes.\n'
        #message = click.edit(marker)
        #if message is None or message.strip() == marker.strip():
        raise click.ClickException("Aborting checkin due to empty message.")
        #message = "".join(message.splitlines(True)[1:]) # Remove marker
    
    k.checkin(message.strip())

@kelp.command()
@click.pass_obj
def log(k: Kelp):
    """Show the checkin history."""
    k.log()

# --- Event/Ticket Command Group ---
@kelp.group()
def event():
    """Manage events (tickets/issues)."""
    pass

@event.command(name="make")
@click.argument("title")
@click.pass_context
def event_make(ctx, title: str):
    """Make a new event."""
    k: Kelp = ctx.obj
    description = click.edit()
    if description is None:
        click.echo("Aborted event creation.")
        return
    evt_uuid = k.make_event(title, description)
    click.echo(f"Event created: {evt_uuid[:12]}")

@event.command(name="list")
@click.pass_context
def event_list(ctx):
    """List all events."""
    k: Kelp = ctx.obj
    events = k.list_events()
    click.echo(f"{'ID':<13}{'Status':<8}{'Title'}")
    click.echo("-" * 60)
    for ev in events:
        click.echo(f"{ev['evt_uuid'][:12]:<13}{ev['status']:<8}{ev['title']}")

@event.command(name="show")
@click.argument("evt_prefix")
@click.pass_context
def event_show(ctx, evt_prefix: str):
    """Show details for a specific event."""
    k: Kelp = ctx.obj
    event_row, content = k.show_event(evt_prefix)
    click.echo(f"Event: {event_row['evt_uuid']}")
    click.echo(f"Title: {event_row['title']}")
    click.echo(f"Status: {event_row['status']}")
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event_row['evt_mtime']))
    click.echo(f"Last Modified: {ts}")
    click.echo("-" * 20)
    click.echo(content)

@event.command(name="edit")
@click.argument("evt_prefix")
@click.option("--title", "-t", help="Set a new title for the event.")
@click.option("--status", "-s", help="Set a new status (e.g., 'open', 'closed').")
@click.option("--comment", "-m", is_flag=True, help="Edit the event's main description in your editor.")
@click.pass_context
def event_edit(ctx, evt_prefix: str, title: Optional[str], status: Optional[str], comment: bool):
    """Edit the properties of an existing event."""
    k: Kelp = ctx.obj
    k._ensure_repo()

    if not any([title, status, comment]):
        click.echo("No edit specified. Use --title, --status, or --comment.", err=True)
        # To show the help message, you can invoke it like this:
        # click.echo(ctx.get_help())
        return

    new_comment_content: Optional[str] = None
    if comment:
        try:
            # Get the current content to prepopulate the editor
            _event_row, current_content = k.show_event(evt_prefix)
            edited_content = click.edit(current_content)
            
            # click.edit returns None if the user quits without saving
            if edited_content is not None:
                new_comment_content = edited_content

        except click.ClickException as e:
            # Pass through errors like "event not found"
            raise e
    
    try:
        k.update_event(
            evt_prefix=evt_prefix, 
            new_title=title, 
            new_status=status, 
            new_comment=new_comment_content
        )
    except click.ClickException as e:
        raise e

@event.command(name="link")
@click.argument("evt_prefix")
@click.argument("checkin_prefix")
@click.pass_context
def event_link(ctx, evt_prefix: str, checkin_prefix: str):
    """Manually link an event to a check-in."""
    k: Kelp = ctx.obj
    k.link_event_to_checkin(evt_prefix, checkin_prefix)

@kelp.command()
@click.argument("revisions", nargs=-1)
@click.pass_obj
def diff(k: Kelp, revisions: Tuple[str, ...]):
    """
    Show changes between checkins.
    
    - `kelp diff`: show changes in HEAD vs its parent.
    - `kelp diff <rev>`: show changes in <rev> vs its parent.
    - `kelp diff <old> <new>`: show changes between two revisions.
    """
    k._ensure_repo()

    if len(revisions) > 2:
        raise click.ClickException("`diff` command accepts at most two revisions.")

    try:
        if len(revisions) == 0:
            # Case: kelp diff (HEAD vs parent)
            new_uuid = k._get_current_checkin_uuid()
            if not new_uuid:
                raise click.ClickException("No checkins yet. Nothing to diff.")
            old_uuid = k._get_parent_uuid(new_uuid) or ROOT_CHECKIN_UUID
        
        elif len(revisions) == 1:
            # Case: kelp diff <rev> (<rev> vs parent)
            new_uuid = k._get_full_uuid(revisions[0], "checkin", "checkin_uuid")
            old_uuid = k._get_parent_uuid(new_uuid) or ROOT_CHECKIN_UUID

        else: # len(revisions) == 2
            # Case: kelp diff <old> <new>
            old_uuid = k._get_full_uuid(revisions[0], "checkin", "checkin_uuid")
            new_uuid = k._get_full_uuid(revisions[1], "checkin", "checkin_uuid")

        k.diff(old_uuid, new_uuid)

    except click.ClickException as e:
        # Re-raise click exceptions to show clean error messages
        raise e
    except Exception as e:
        # Catch other potential errors for robustness
        raise click.ClickException(f"An error occurred during diff: {e}")

@kelp.command()
@click.option('--port', default=8080, help='Port to run the web server on.')
@click.pass_context
def ui(ctx, port):
    """Launch the Kelp web interface."""
    from .ui import run_ui
    try:
        k = Kelp()
        k._ensure_repo()
        run_ui(repo_path=".", port=port)
    except click.ClickException as e:
        raise e
    except ImportError:
        raise click.ClickException("Flask is required for the UI. Please run 'pip install Flask'.")
    except Exception as e:
        raise click.ClickException(f"Failed to start UI: {e}")



if __name__ == "__main__":
    kelp()
