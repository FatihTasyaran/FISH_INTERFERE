"""In-place sanitize all 17 A2 sheets: read every cell, escape leading +/-/=/@,
write back. No source-data re-extraction needed."""
import sys
sys.path.insert(0, '/home/tue037807/fish_interfere/scripts')
from pbr1_sheet_writer import make_clients

SHEET_IDS = [
    ('APRILTAG-NODE-A2',        '1l6QG5pkZWOP2P6Ya1kP5-UNyxZR46GB976PDoxAepas'),
    ('IMAGE_PROC-NODE-A2',      '1ZwErVZTf_B7fnasql7ac98uua1C1Joym-cg7WHX-rtc'),
    ('STEREO_IMAGE_PROC-NODE-A2','1lOexNCEDDprYXfY2z_FEDN_OT-LHXG-x4SjPoyJ_3s4'),
    ('DNN_IMAGE_ENCODER-NODE-A2','1s5NFe5OmOc8pyG3Xcemq9Glfi-tBcK5WkmXfreBC1_g'),
    ('VISUAL_SLAM-NODE-A2',     '1ZTy8b3HNmpO_zO5WLorfNOxdYTo8m7mnmd2t7hurwgI'),
    ('NVBLOX-NODE-A2',          '10oI0z2JvDepAvW119ibYnh2ex3aYhiq1SxkZFddRPFo'),
    ('DETECTNET-NODE-A2',       '1uIKovUQtF9d_I2O-JUjl-xeHtM8HEwH_MbouIrL6DUI'),
    ('BI3D-NODE-A2',            '1gygp8EdJB1nqVlBiSZoeSQf_1Gia7YOoxp2zwW03aa4'),
    ('ESS-NODE-A2',             '1XjLRIGoypoaspi8Mpko0i7TYbvCREBuUo02D7Yz2hkU'),
    ('SEGFORMER-NODE-A2',       '11M0c6nXx4wpRnCXh7-PYVBLz3YiIoIjxmju7iOud0kQ'),
    ('UNET-NODE-A2',            '1HPByMWbmEc1yMy0rnQ6iA2vM2PVbKicuPzseOJ-WlVA'),
    ('TENSOR_RT-NODE-A2',       '1PBf16XOhNbPtWSqLmc1d7Z83azGqYRswCw8maw0S6hc'),
    ('TRITON-NODE-A2',          '1KwGxz46t6t27cufkbRVYgi6ZXvhsPm1naqmXgt41lII'),
    ('BI3D_FREESPACE-NODE-A2',  '1ds2-dRda2m-LRs-ZCXYV4Y0w7zDHhSUjeNzTX_T-f-c'),
    ('RTDETR-NODE-A2',          '1UwaMBvFlDFh-kr2gVfNXxDilOYxVlFZtYzAoyPwq4UY'),
    ('NITROS_BRIDGE-NODE-A2',   '16kT9uTpBCIw0sCV5tYPnPQeMJWHuu3HFKlXITzPr_jk'),
    ('DOPE-NODE-A2',            '1Hsrai5lqdkXHt0dmUqB_mG6hfBP_bsMnKW0XyUOE2LE'),
]


def sanitize(v):
    if isinstance(v, str) and len(v) > 0 and v[0] in ('+', '-', '=', '@'):
        return "'" + v
    return v


def fix_sheet(sheets, sid, title):
    # Get all sheet (tab) titles
    meta = sheets.spreadsheets().get(spreadsheetId=sid, fields='sheets(properties(title))').execute()
    tab_titles = [s['properties']['title'] for s in meta.get('sheets', [])]

    # Batch read all tabs in ONE request
    ranges = [f"'{t}'" for t in tab_titles]
    resp = sheets.spreadsheets().values().batchGet(
        spreadsheetId=sid, ranges=ranges, valueRenderOption='FORMULA',
    ).execute()

    fixed_tabs = 0
    fixed_cells = 0
    batch_writes = []
    for vr in resp.get('valueRanges', []):
        rng = vr.get('range')
        values = vr.get('values', [])
        if not values:
            continue

        new_values = []
        any_fix = False
        for row in values:
            new_row = []
            for cell in row:
                fixed = sanitize(cell) if isinstance(cell, str) else cell
                if fixed != cell:
                    any_fix = True
                    fixed_cells += 1
                new_row.append(fixed)
            new_values.append(new_row)

        if any_fix:
            batch_writes.append({'range': rng, 'majorDimension': 'ROWS', 'values': new_values})
            fixed_tabs += 1

    if batch_writes:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sid,
            body={'valueInputOption': 'USER_ENTERED', 'data': batch_writes},
        ).execute()

    return fixed_tabs, fixed_cells


def main():
    import time
    sheets, _drive = make_clients()
    for title, sid in SHEET_IDS:
        print(f"--- {title} ---", flush=True)
        try:
            tab_count, cell_count = fix_sheet(sheets, sid, title)
            print(f"  fixed {tab_count} tabs / {cell_count} cells", flush=True)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
        # Rate limit: 60 read req/min — sleep 5s between sheets (~12 sheets/min max)
        time.sleep(5)


if __name__ == '__main__':
    main()
