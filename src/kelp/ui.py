# ui.py
import time
import difflib
import webbrowser
from threading import Timer
from flask import Flask, render_template, abort

REPO_PATH = "." 
app = Flask(__name__)

@app.template_filter('strftime')
def _jinja2_filter_datetime(timestamp):
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(timestamp))

# ---- Side-by-Side Diff Helper ----
def create_side_by_side_diff(from_text, to_text):
    """Generates data structured for a side-by-side diff template."""
    from_lines = from_text.splitlines()
    to_lines = to_text.splitlines()
    matcher = difflib.SequenceMatcher(None, from_lines, to_lines)
    diff_lines = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i in range(i1, i2):
                diff_lines.append((i + 1, from_lines[i], i + 1, to_lines[i - i1 + j1], 'equal'))
        else:
            if tag == 'replace' or tag == 'delete':
                for i in range(i1, i2):
                    diff_lines.append((i + 1, from_lines[i], '', '', 'delete'))
            if tag == 'replace' or tag == 'insert':
                for j in range(j1, j2):
                    diff_lines.append(('', '', j + 1, to_lines[j], 'insert'))
    return diff_lines


@app.route('/')
def timeline():
    from .main import Kelp
    k = Kelp(REPO_PATH)
    k._ensure_repo()
    checkins_raw = k.get_log() 
    processed_checkins = []
    for checkin in checkins_raw:
        # Convert the immutable sqlite3.Row to a mutable dict
        checkin_dict = dict(checkin)
        
        linked_events = []
        events_str = checkin_dict.get('linked_events_str')
        if events_str:
            event_pairs = events_str.split('||')
            for pair in event_pairs:
                # Split 'uuid|title' into parts
                parts = pair.split('|', 3)
                if len(parts) == 4:
                    linked_events.append({'uuid': parts[0], 'title': parts[1], 'link_type': parts[2], 'mtime': int(parts[3])})
        
        checkin_dict['linked_events'] = linked_events
        processed_checkins.append(checkin_dict)
        
    return render_template('timeline.html', title="Timeline", checkins=processed_checkins)

@app.route('/checkin/<checkin_uuid>')
def checkin_view(checkin_uuid):
    from .main import Kelp
    k = Kelp(REPO_PATH)
    k._ensure_repo()

    checkin_details = k.get_checkin_details(checkin_uuid)
    if not checkin_details:
        abort(404, "Checkin not found")

    linked_events = k.get_linked_events(checkin_uuid)
    current_checkin_ts = checkin_details['checkin']['timestamp']
    context_checkins = k.get_checkin_context(current_checkin_ts)
    return render_template('checkin.html', 
                           title=f"Checkin {checkin_uuid[:8]}", 
                           checkin=checkin_details['checkin'], 
                           files=checkin_details['files'],
                           context_checkins=context_checkins,
                           linked_events=linked_events)

@app.route('/events')
def event_list_view():
    from .main import Kelp
    k = Kelp(REPO_PATH)
    k._ensure_repo()
    events = k.list_events()
    return render_template('events.html', title="Events", events=events)

@app.route('/event/<evt_uuid>')
def event_detail_view(evt_uuid):
    from .main import Kelp
    k = Kelp(REPO_PATH)
    k._ensure_repo()
    
    event_row, content = k.show_event(evt_uuid)
    if not event_row:
        abort(404, "Event not found.")


    linked_checkins = k.get_linked_checkins(evt_uuid)
    event_log = k.get_event_log(evt_uuid)
    
    return render_template('event_detail.html', 
                           title=f"Event {evt_uuid[:8]}", 
                           event=event_row, 
                           content=content,
                           log=event_log,
                           linked_checkins=linked_checkins)



def run_ui(repo_path=".", port=8080):
    """Sets repo path and runs the Flask app."""
    global REPO_PATH
    REPO_PATH = repo_path
    
    # Open browser after a short delay
    Timer(1, lambda: webbrowser.open_new(f"http://127.0.0.1:{port}")).start()
    
    print(f"Starting Kelp UI at http://127.0.0.1:{port}")
    print("Use Ctrl+C to stop the server.")
    app.run(port=port, debug=True)
