#!/usr/bin/env python3
"""per-bench-routine-1 sheet writer via direct Google Sheets API.

For a given benchmark name, reads the payload from pbr1_sheet_builder, creates
the spreadsheet under the FISH Sheets folder, and writes every tab in a single
batchUpdate call. Idempotent: if a spreadsheet with the target title already
exists in the folder it is REUSED (sheets are cleared + re-written).

Usage:
  python3 pbr1_sheet_writer.py <benchmark_name>
"""
import sys, os, json, subprocess
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

OAUTH = '/home/tue037807/oauth-token.json'
FOLDER = '1oolc-au5UPhki7TaF1qMX6Q7Da-EyNL0'  # FISH Sheets

def make_clients():
    creds = Credentials.from_authorized_user_file(OAUTH)
    sheets = build('sheets', 'v4', credentials=creds)
    drive = build('drive', 'v3', credentials=creds)
    return sheets, drive

def find_existing(drive, title):
    q = f"name = '{title}' and '{FOLDER}' in parents and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    res = drive.files().list(q=q, fields='files(id,name)').execute()
    files = res.get('files', [])
    return files[0]['id'] if files else None

def get_or_create(sheets, drive, title):
    sid = find_existing(drive, title)
    if sid:
        return sid, True
    body = {'properties': {'title': title}}
    sp = sheets.spreadsheets().create(body=body).execute()
    sid = sp['spreadsheetId']
    # Move into folder
    drive.files().update(fileId=sid, addParents=FOLDER, removeParents='root',
                         fields='id, parents').execute()
    return sid, False

def list_tabs(sheets, sid):
    sp = sheets.spreadsheets().get(spreadsheetId=sid, fields='sheets(properties(sheetId,title))').execute()
    return [(s['properties']['sheetId'], s['properties']['title']) for s in sp.get('sheets', [])]

def write_payload(sheets, drive, payload):
    title = payload['spreadsheet_title']
    sid, existed = get_or_create(sheets, drive, title)
    print(f"  {'reuse' if existed else 'create'} {title}  id={sid}", flush=True)
    existing = list_tabs(sheets, sid)
    by_title = {t: tid for tid, t in existing}
    # Figure out add vs reuse
    target_titles = [t['name'] for t in payload['tabs']]
    requests = []
    # Delete any tabs not in target_titles, EXCEPT keep "Sheet1" as a placeholder
    for tid, t in existing:
        if t == 'Sheet1' and target_titles and target_titles[0] not in by_title:
            # Will rename Sheet1 to first target instead of delete
            continue
        if t not in target_titles:
            requests.append({'deleteSheet': {'sheetId': tid}})
    # Rename or create tabs in order
    used_sheet1 = False
    sheet1_id = next((tid for tid, t in existing if t == 'Sheet1'), None)
    for i, tab in enumerate(payload['tabs']):
        name = tab['name']
        if name in by_title:
            continue
        if not used_sheet1 and sheet1_id is not None:
            requests.append({'updateSheetProperties': {'properties': {'sheetId': sheet1_id, 'title': name, 'index': i}, 'fields': 'title,index'}})
            used_sheet1 = True
            by_title[name] = sheet1_id
        else:
            requests.append({'addSheet': {'properties': {'title': name, 'index': i}}})
    if requests:
        resp = sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': requests}).execute()
        # capture new sheet IDs
        for r in resp.get('replies', []):
            if 'addSheet' in r:
                ps = r['addSheet']['properties']
                by_title[ps['title']] = ps['sheetId']
    # Build a batch values update
    data = []
    for tab in payload['tabs']:
        # Convert each row's cells to strings (Sheets API expects strings or numbers; lists/objs aren't accepted)
        rows = []
        for row in tab['rows']:
            out = []
            for cell in row:
                if cell is None:
                    out.append('')
                elif isinstance(cell, (str, int, float, bool)):
                    out.append(cell)
                else:
                    out.append(str(cell))
            rows.append(out)
        data.append({
            'range': f"'{tab['name']}'!A1",
            'majorDimension': 'ROWS',
            'values': rows,
        })
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
        'valueInputOption': 'USER_ENTERED',
        'data': data,
    }).execute()
    print(f"  wrote {len(payload['tabs'])} tabs", flush=True)
    return sid

def main():
    if len(sys.argv) < 2:
        print('usage: pbr1_sheet_writer.py <benchmark_name> [<benchmark_name> ...]', file=sys.stderr)
        sys.exit(1)
    sheets, drive = make_clients()
    for name in sys.argv[1:]:
        print(f"=== {name} ===", flush=True)
        # Generate payload via the builder
        proc = subprocess.run(
            ['python3', '/home/tue037807/fish_interfere/scripts/pbr1_sheet_builder.py', name],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            print(f"  builder FAILED rc={proc.returncode}: {proc.stderr[:500]}", flush=True)
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            print(f"  builder produced bad JSON: {e}; stdout head={proc.stdout[:300]}", flush=True)
            continue
        try:
            sid = write_payload(sheets, drive, payload)
            print(f"  → https://docs.google.com/spreadsheets/d/{sid}", flush=True)
        except HttpError as e:
            print(f"  ERROR {type(e).__name__}: {e}", flush=True)

if __name__ == '__main__':
    main()
